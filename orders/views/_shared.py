"""Helpers shared across the order views.

Extracted from the 5,919-line `orders/views.py` as the first step of splitting
it (plan item 3.1). Nothing here is order-flow logic — these are the scoping
predicates that several unrelated view groups needed, and being needed by
everything is part of what kept the file indivisible.

These belong in `orders/selectors.py` eventually. They are here first because a
move within `orders.views` is invisible to every caller, and a move to a new
top-level module is not.
"""
from orders.models import OrdersLog, Order, OrderStatus
from django.db.models import Q
from sap_sync.models import Party as SapParty
from users.models import User
import logging

logger = logging.getLogger(__name__)



def _get_user_category_name(user):
    category_obj = getattr(user, 'category', None)
    category_name = getattr(category_obj, 'category', category_obj)
    normalized_category = str(category_name or '').strip().upper()
    return normalized_category or None


def _get_user_category_names(user):
    category = _get_user_category_name(user)
    return [category] if category else []


def _normalize_scope_name(value):
    normalized = str(value or '').strip()
    return normalized or None


def _get_user_main_group_names(user):
    names = []
    main_group = getattr(user, 'main_group', None)
    if main_group:
        names.append(_normalize_scope_name(getattr(main_group, 'name', main_group)))

    main_groups = getattr(user, 'main_groups', None)
    if main_groups is not None:
        for group in main_groups.all():
            names.append(_normalize_scope_name(getattr(group, 'name', group)))

    return list(dict.fromkeys(name for name in names if name))


def _build_iexact_filter(field_name, values):
    query = Q()
    for value in values:
        query |= Q(**{f'{field_name}__iexact': value})
    return query

def _assigned_rate_approvers_for_order(order, status_filter=None, exclude_user=None):
    approvals = order.rate_approvals.select_related('approver').all()
    if status_filter:
        approvals = approvals.filter(status=status_filter)
    if exclude_user:
        approvals = approvals.exclude(approver=exclude_user)
    return [approval.approver for approval in approvals if approval.approver]




BILLING_ACTIVE_CODES = ['BILLING', 'BILLING_PENDING']
BILLING_ACCEPTED_ACTION_ID = 3
BILLING_REJECTED_ACTION_ID = 8
BILLING_DECISION_ACTION_IDS = [BILLING_ACCEPTED_ACTION_ID, BILLING_REJECTED_ACTION_ID]
AUDITOR_ACCEPTED_ACTION_ID = 9
AUDITOR_REJECTED_ACTION_ID = 7
AUDITOR_DECISION_ACTION_IDS = [AUDITOR_ACCEPTED_ACTION_ID, AUDITOR_REJECTED_ACTION_ID]
APPROVER_ACTIVE_CODES = ['NEED_APPROVAL', 'RATE_APPROVAL']


def _apply_billing_order_scope(queryset, user):
    user_categories = _get_user_category_names(user)
    user_main_groups = _get_user_main_group_names(user)

    if not user_categories and not user_main_groups:
        return queryset

    party_queryset = SapParty.objects.all()
    if user_categories:
        party_queryset = party_queryset.filter(category__in=user_categories)
    if user_main_groups:
        party_queryset = party_queryset.filter(
            _build_iexact_filter('main_group', user_main_groups)
        )

    queryset = queryset.filter(
        card_code__in=party_queryset.values_list('card_code', flat=True)
    )

    if user_categories:
        queryset = queryset.filter(items__category__in=user_categories)

    return queryset.distinct()

def _get_base_orders(user):
    """Scope orders by user role:
    - admin: all orders
    - manager: only orders created by this user
    - distributor: only the distributor's own orders (same scope as manager, so
      the dashboard shows all sections but only this distributor's data)
    - mart_approval: all distributor (company-3 / Mart) orders — the Mart queue
      this role manages end to end
    - auditor: orders currently in or previously routed through auditor review
    - approver: orders pending approval (NEED_APPROVAL, RATE_APPROVAL)
    - billing: orders currently in billing or already handled by this billing user
    """
    role = getattr(user, 'role', None)
    role_name = getattr(role, 'name', '').lower() if role else ''
    if role_name == 'admin':
        return Order.objects.all()
    if role_name in ('manager', 'distributor'):
        return Order.objects.filter(created_by=user.id)
    if role_name == 'mart_approval':
        return Order.objects.filter(order_type='DISTRIBUTOR')
    if role_name == 'auditor':
        return Order.objects.filter(
            Q(status__code='AUDITOR_APPROVAL') |
            Q(logs__action__code='AUDITOR_APPROVAL') |
            Q(logs__action__name__icontains='auditor')
        ).distinct()
    if role_name == 'approver':
        queryset = Order.objects.filter(
            rate_approvals__approver=user,
            rate_approvals__status='PENDING',
            status__code__in=APPROVER_ACTIVE_CODES,
        ).distinct()
        return queryset
    if role_name == 'billing':
        handled_order_ids = (
            OrdersLog.objects
            .filter(
                performed_by=user,
                action_id__in=BILLING_DECISION_ACTION_IDS
            )
            .values_list('order_id', flat=True)
            .distinct()
        )

        queryset = Order.objects.filter(
            Q(status__code__in=BILLING_ACTIVE_CODES) |
            Q(id__in=handled_order_ids)
        ).distinct()

        return _apply_billing_order_scope(queryset, user)
    return Order.objects.none()



# OrderStatus rows already present in the DB (managed there, not via migration):
#   Mart Approval = 12 (where a submitted distributor order lands)
#   Approved      = 6  (on approve)
#   Rejected      = 7  (on reject)
MART_STATUS_PENDING_ID = 12


def _mart_status(status_id):
    """Fetch a Mart-flow status by id (rows are seeded in the DB, not migrations)."""
    return OrderStatus.objects.filter(id=status_id).first()


def _mart_approver_user():
    """The user who works the Mart Approval queue. There is a single
    'mart_approval' user in this deployment, so a freshly submitted distributor
    order's pending Mart-Approval log row is stamped with that user instead of
    NULL. Falls back to None if the role/user isn't present."""
    # Match the role loosely (case / underscore / space insensitive) so a role
    # stored as 'Mart Approval' or 'mart_approval' both resolve.
    return (
        User.objects
        .filter(is_active=True)
        .filter(Q(role__name__iexact='mart_approval') |
                Q(role__name__iexact='mart approval'))
        .order_by('id')
        .first()
    )
