"""Rate approval: who must approve an off-price order line, and why.

Lifted out of `orders/views.py` (plan item 3.2). Two decisions live here:

* whether a line needs approval at all — `_get_rate_approval_reason` compares
  the price list basic against the entered basic, with deliberate exemptions
  for scheme lines and the free half of a combo pack, which are priced at zero
  by design and must not drag the whole order into approval;
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
from users.models import User


APPROVER_ACCEPTED_ACTION_ID = 6
APPROVER_REJECTED_ACTION_ID = 7
APPROVER_DECISION_ACTION_IDS = [APPROVER_ACCEPTED_ACTION_ID, APPROVER_REJECTED_ACTION_ID]

def _rate_approval_remarks(flagged_items):
    return '; '.join(flagged_items) or 'Rate approval required by admin price condition'

def _get_rate_approval_reason(item, price_list_basic, basic_price):
    if item.get('item_type') == 'SCHEME':
        return None

    # The free half of a combo pack is priced at 0 by design, so the 0-vs-0
    # comparison below must not drag the whole order into rate approval.
    if str(item.get('is_auto_free', '')).lower() in ('true', '1'):
        return None

    if item.get('qty') not in (None, ''):
        try:
            qty = float(item.get('qty') or 0)
        except (TypeError, ValueError):
            return None
        if qty <= 0:
            return None

    item_name = item.get('item_name') or item.get('item_code') or 'Item'

    if price_list_basic == 0 and basic_price == 0:
        return f"{item_name}: Price List (Basic) Rs {price_list_basic} = Basic Rs {basic_price}"
    if price_list_basic == 0 and basic_price > 0:
        return f"{item_name}: Price List (Basic) Rs {price_list_basic} < Basic Rs {basic_price}"
    if price_list_basic > basic_price:
        return f"{item_name}: Price List (Basic) Rs {price_list_basic} > Basic Rs {basic_price}"
    if price_list_basic < basic_price:
        return f"{item_name}: Price List (Basic) Rs {price_list_basic} < Basic Rs {basic_price}"
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
    fall back to the synced SAP product (sap_products) by item_code (and category)."""
    stored = str(getattr(item, 'sub_group', '') or '').strip()
    if stored:
        return stored

    item_code = str(getattr(item, 'item_code', '') or '').strip()
    if not item_code:
        return ''

    category = str(getattr(item, 'category', '') or '').strip()
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
