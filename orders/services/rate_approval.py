"""Rate approval: who must approve an off-price order line, and why.

Lifted out of `orders/views.py` (plan item 3.2). Two decisions live here:

* whether a line needs approval at all — `_get_rate_approval_reason` compares
  the entered basic against the rate AGREED WITH THIS PARTY on
  `party_product_assignments`, and asks for a signature only when the line
  sells below it;
* who is asked — `assign_rate_approvers` resolves approvers per item from the
  configured rules, so an order can end up with several, each deciding
  independently.

Both were reached only through a view before, which meant testing "does this
line need approval" required placing an order over HTTP.
"""
from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone

from orders.models import (
    OrderItemApprovalMapping,
    OrderRateApproval,
    OrdersLog,
    RateApproverRule,
)
from sap_sync.models import Product as SapProduct
from users.models import PartyProductAssignment, User


APPROVER_ACCEPTED_ACTION_ID = 6
APPROVER_REJECTED_ACTION_ID = 7
APPROVER_DECISION_ACTION_IDS = [APPROVER_ACCEPTED_ACTION_ID, APPROVER_REJECTED_ACTION_ID]

# Rates are stored to 4dp, and a tax-inclusive round trip lands on 3549.9999
# rather than 3550, so an exact comparison misfires on correctly-priced lines.
RATE_APPROVAL_TOLERANCE = 0.05

# Sub groups sold as commodities. Their price moves with the market, so parties
# carry no fixed agreed rate for them -- `basic_rate` sits at 0 on
# `party_product_assignments`. That zero is "no rate agreed", not "free", so a
# commodity line still needs a signature. Mirrors the commodity list in
# OrderItemSerializer.get_variety_type, which classifies OLIVE as PREMIUM.
COMMODITY_SUB_GROUPS = {
    'BLENDED',
    'COTTON SEED',
    'GIFT PACK',
    'GROUNDNUT',
    'MUSTARD',
    'PALMOLEIN',
    'RICE BRAN',
    'SESAME',
    'SOYABEAN',
    'SUNFLOWER',
}


def _rate_approval_remarks(flagged_items):
    return '; '.join(flagged_items) or 'Rate approval required by admin price condition'


def _item_field(item, *names):
    """Read a field off either an OrderItem instance or a raw payload dict.

    The order-create path works with the request's dicts before the rows
    exist, while the approver-matching path works with saved OrderItem
    objects. Both need the same sub-group lookup, so accept either shape.
    """
    for name in names:
        value = item.get(name) if isinstance(item, dict) else getattr(item, name, None)
        if value not in (None, ''):
            return str(value).strip()
    return ''


def _is_commodity_item(item):
    return _get_item_sub_group(item).upper() in COMMODITY_SUB_GROUPS


def _authorised_basic_rate(card_code, item_code, category):
    """The basic rate this party is authorised to be charged for this item.

    Returns None when no active assignment exists. One query per line; orders
    average two or three lines, so this is not worth batching yet.
    """
    if not (card_code and item_code):
        return None
    return (
        PartyProductAssignment.objects
        .filter(card_code=card_code, item_code=item_code, category=category, is_active=True)
        .values_list('basic_rate', flat=True)
        .first()
    )

def _get_rate_approval_reason(item, authorised_rate, basic_price):
    """Why this line needs rate approval, or None.

    Approval means one thing: the line is being sold BELOW the rate agreed with
    this party on `party_product_assignments`. Selling at or above it is not a
    concession and needs nobody's signature.

    It deliberately does NOT compare against the `price_list_basic` the form
    sends. That field is built two different ways -- sometimes the agreed rate,
    sometimes that rate plus tax (`computeLandingPrice` in the order form) --
    so comparing a tax-inclusive figure against a pre-tax one flagged every
    correctly-priced order. Between 28 Jul and 27 Aug that was 94% of orders
    (613 of 647). The assignment is also the real authority: the form's Price
    List field can be edited by the salesperson.
    """
    if item.get('qty') not in (None, ''):
        # Guarded parse: a malformed qty must not 500 the whole order-create.
        try:
            qty = float(item.get('qty') or 0)
        except (TypeError, ValueError):
            return None
        if qty <= 0:
            return None

    # A zero-priced line is a giveaway -- a scheme companion, the free half of
    # a combo, or an FOC order. There is no discount to approve. This also
    # covers what the old `item_type == 'SCHEME'` and `is_auto_free` guards
    # were reaching for; the first never fired, because item_type holds the
    # pack size ('1 LTR', '5 LTR', ...), never 'SCHEME'.
    if basic_price <= 0:
        return None

    # No rate agreed for this party and item. Nothing to measure against, so
    # nothing to approve -- these are unmapped master data rather than
    # discounts, and at ~43% of lines they buried the real ones. They want a
    # report of unmapped party/item pairs, not an approval queue.
    if authorised_rate is None:
        return None
    authorised_rate = float(authorised_rate)
    if authorised_rate <= 0:
        # A zero agreed rate means no rate was ever agreed, not that the item is
        # free, so there is no benchmark the line can be said to clear. It now
        # goes for approval whatever the sub group.
        #
        # This used to exempt everything except the commodity sub groups, on the
        # grounds that a zero elsewhere was unmapped master data rather than a
        # discount. That reasoning let ORD-20260912-0007 through: both lines
        # (CANOLA, OLIVE -- both PREMIUM) had basic_rate 0 on the party's
        # assignments, so neither flagged, the order skipped Rate Approval
        # entirely and landed on the auditor, who rejected it with "need
        # approval". One of those lines was 100 x EXTRA LIGHT OLIVE 5 LTR at
        # Rs 0.0010. Nothing in the flow looked at it.
        #
        # Priced lines with no agreed rate are ~63% of recent lines (1218 of
        # 1925 over 60 days), so this sends materially more orders to Rate
        # Approval until `party_product_assignments.basic_rate` is populated.
        # That is the intended trade: an unmapped pair is exactly the case where
        # nobody has agreed a price, and it is the salesperson's own number that
        # would otherwise stand unchecked.
        item_name = item.get('item_name') or item.get('item_code') or 'Item'
        if _is_commodity_item(item):
            return (f"{item_name}: commodity sold at Rs {basic_price} with no "
                    f"agreed rate on record")
        return (f"{item_name}: sold at Rs {basic_price} with no agreed rate "
                f"on record")

    if basic_price < authorised_rate - RATE_APPROVAL_TOLERANCE:
        item_name = item.get('item_name') or item.get('item_code') or 'Item'
        return (f"{item_name}: Basic Rs {basic_price} < agreed rate "
                f"Rs {authorised_rate}")

    return None


def _with_latest_approver_decision(orders, user):
    latest_approver_action = (
        OrdersLog.objects
        .filter(
            order=OuterRef('pk'),
            performed_by=user,
            action_id__in=APPROVER_DECISION_ACTION_IDS,
        )
        .order_by('-created_at', '-id')
        .values('action_id')[:1]
    )
    return orders.annotate(
        latest_approver_action_id=Subquery(latest_approver_action)
    )

def _get_order_rate_approval(order, user):
    if not user or not getattr(user, 'is_authenticated', False):
        return None
    return OrderRateApproval.objects.filter(order=order, approver=user).first()

def _has_pending_rate_approvals(order):
    return order.rate_approvals.filter(status='PENDING').exists()

def _mark_rate_approval_decision(order, user, decision, remarks=''):
    approval = _get_order_rate_approval(order, user)
    if not approval:
        return None

    approval.status = decision
    approval.remarks = remarks or ''
    approval.approved_at = timezone.now()
    approval.save(update_fields=['status', 'remarks', 'approved_at'])
    return approval

def _get_item_sub_group(item):
    """Resolve an order item's sub group. Prefer the value stored on the order item;
    fall back to the synced SAP product (sap_products) by item_code (and category).

    Reads through `_item_field` because the commodity check above runs on the
    order-create path, where an item is still a request dict rather than a
    saved `OrderItem`. `variety` is accepted as an alias: that is what the
    order form calls this field.
    """
    stored = _item_field(item, 'sub_group', 'variety')
    if stored:
        return stored

    item_code = _item_field(item, 'item_code')
    if not item_code:
        return ''

    category = _item_field(item, 'category')
    product_query = SapProduct.objects.filter(item_code__iexact=item_code)
    if category:
        product_query = product_query.filter(category__iexact=category)

    product = product_query.first()
    return str(getattr(product, 'sub_group', '') or '').strip() if product else ''


def _get_rate_approvers_for_item(item):
    category = str(getattr(item, 'category', '') or '').strip()
    sub_group = _get_item_sub_group(item)
    if not category:
        return []

    rule_query = RateApproverRule.objects.filter(
        category__iexact=category,
        is_active=True,
    )
    if sub_group:
        rule = rule_query.filter(sub_group__iexact=sub_group).select_related('approver').first()
        if not rule:
            rule = (
                rule_query
                .filter(Q(sub_group__isnull=True) | Q(sub_group=''))
                .select_related('approver')
                .first()
            )
    else:
        rule = (
            rule_query
            .filter(Q(sub_group__isnull=True) | Q(sub_group=''))
            .select_related('approver')
            .first()
        )
    if rule and rule.approver:
        return [rule.approver]

    users = User.objects.filter(
        role__name__iexact='approver',
        is_active=True,
        category__category__iexact=category,
    )

    matched_users = []
    for user in users:
        user_sub_groups = [
            value.strip().lower()
            for value in str(getattr(user, 'sub_group', '') or '').split(',')
            if value.strip()
        ]
        if not user_sub_groups:
            matched_users.append(user)
            continue
        if sub_group and sub_group.lower() in user_sub_groups:
            matched_users.append(user)
    
    print(f"Matched approvers for item {getattr(item, 'item_code', '')} (category: {category}, sub_group: {sub_group}): {[user.username for user in matched_users]}")
    print("\n=======================================================================================================================================================\n")

    return matched_users

def assign_rate_approvers(order):
    """
    Create OrderRateApproval and OrderItemApprovalMapping
    based on item sub group.
    """
  
    OrderItemApprovalMapping.objects.filter(order=order).delete()

    approvers = set()

    for item in order.items.all():

        for approver in _get_rate_approvers_for_item(item):
            approvers.add(approver)

            OrderItemApprovalMapping.objects.get_or_create(
                order=order,
                order_item=item,
                approver=approver
            )

    approver_ids = {approver.id for approver in approvers}

    # Drop approvals only for approvers no longer assigned to the order.
    OrderRateApproval.objects.filter(order=order).exclude(
        approver_id__in=approver_ids
    ).delete()

    # Add rows for newly assigned approvers; leave existing decisions untouched.

    for approver in approvers:
        OrderRateApproval.objects.get_or_create(
            order=order,
            approver=approver,
            defaults={
                "status": "PENDING"
            }
        )
