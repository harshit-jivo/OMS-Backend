"""Dashboard and reporting views.

Second domain out of `orders/views.py` (plan item 3.1). Read-only: these four
views aggregate orders for the web and warehouse dashboards and write nothing,
which is why they were safe to move before the order-flow core.

The state helpers below travelled with them because nothing else uses them —
the state map exists to label dashboard rows by party state, not to drive any
order decision. The status-code constants and `_get_base_orders` did NOT
travel: the order flow reads those too, so they now live in `._shared`.
"""
from urllib import request
from orders.models import OrdersLog, Parties, Order, OrderItem, OrderStatus, OrderRateApproval
from rest_framework.permissions import IsAuthenticated
from core.permissions import HasAnyKey, HasKey
from rest_framework.views import APIView
from rest_framework.response import Response
from datetime import datetime
from functools import lru_cache
import calendar
from django.db.models import Sum, Count, Max, Q, OuterRef, Subquery
from django.db.models.functions import TruncMonth
from django.utils import timezone
from collections import defaultdict
from sap_sync.models import Party as SapParty, PartyAddress as SapPartyAddress, Product as SapProduct
from users.models import User, State
from ._shared import (
    APPROVER_ACTIVE_CODES,
    AUDITOR_ACCEPTED_ACTION_ID,
    AUDITOR_DECISION_ACTION_IDS,
    AUDITOR_REJECTED_ACTION_ID,
    BILLING_ACCEPTED_ACTION_ID,
    BILLING_ACTIVE_CODES,
    BILLING_DECISION_ACTION_IDS,
    BILLING_REJECTED_ACTION_ID,
    _get_base_orders,
    sees_all_orders,
)




def _clean_party_state(value):
    state = str(value or '').strip()
    if not state or state.lower() in {'unknown', 'none', 'null', '-'}:
        return None
    return state


@lru_cache(maxsize=1)
def _get_state_display_map():
    state_map = {}
    for state in State.objects.filter(is_active=True).values('code', 'name'):
        name = _clean_party_state(state.get('name'))
        if not name:
            continue

        for value in (state.get('code'), state.get('name')):
            clean_value = _clean_party_state(value)
            if clean_value:
                state_map[clean_value.lower()] = name

    return state_map


def _format_party_state(value):
    state = _clean_party_state(value)
    if not state:
        return None

    return _get_state_display_map().get(state.lower(), state)


def _build_party_state_map(card_codes):
    normalized_codes = list(dict.fromkeys(
        str(card_code or '').strip()
        for card_code in card_codes
        if str(card_code or '').strip()
    ))
    state_map = {}
    if not normalized_codes:
        return state_map

    for card_code, state in Parties.objects.filter(card_code__in=normalized_codes).values_list('card_code', 'state'):
        clean_state = _format_party_state(state)
        if clean_state:
            state_map[str(card_code).strip()] = clean_state

    missing_codes = [card_code for card_code in normalized_codes if card_code not in state_map]
    if missing_codes:
        for card_code, state in SapParty.objects.filter(card_code__in=missing_codes).values_list('card_code', 'state'):
            clean_state = _format_party_state(state)
            if clean_state:
                state_map.setdefault(str(card_code).strip(), clean_state)

    missing_codes = [card_code for card_code in normalized_codes if card_code not in state_map]
    if missing_codes:
        for card_code, state in SapPartyAddress.objects.filter(card_code__in=missing_codes).values_list('card_code', 'state'):
            clean_state = _format_party_state(state)
            if clean_state:
                state_map.setdefault(str(card_code).strip(), clean_state)

    return state_map


def _build_state_item_sales(filtered_orders, state_map):
    state_item_counts = defaultdict(lambda: {'total_sales': 0, 'quantity': 0, 'boxes': 0, 'ltrs': 0, 'count': 0})

    item_rows = list(OrderItem.objects.filter(order__in=filtered_orders).values(
        'item_code',
        'item_name',
        'category',
        'sub_group',
        'qty',
        'boxes',
        'ltrs',
        'total',
        'order__card_code',
    ))
    product_keys = {
        (
            str(item['item_code'] or '').strip(),
            str(item['category'] or '').strip().upper(),
        )
        for item in item_rows
        if str(item['item_code'] or '').strip()
    }
    products = SapProduct.objects.filter(
        item_code__in=[item_code for item_code, _category in product_keys]
    ).values('item_code', 'category', 'sal_factor2', 'sal_pack_unit')
    product_meta = {}
    for product in products:
        key = (
            str(product.get('item_code') or '').strip(),
            str(product.get('category') or '').strip().upper(),
        )
        product_meta.setdefault(key, product)

    def to_float(value):
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0

    for item in item_rows:
        state = state_map.get(str(item['order__card_code'] or '').strip(), 'Unknown')
        item_code = item['item_code'] or '-'
        item_name = item['item_name'] or item_code
        category = item['category'] or 'Unknown'
        variety = item['sub_group'] or 'Unknown'
        qty = to_float(item['qty'])
        boxes = to_float(item['boxes'])
        ltrs = to_float(item['ltrs'])
        product = product_meta.get((
            str(item_code or '').strip(),
            str(category or '').strip().upper(),
        ))

        if product:
            sal_factor = to_float(product.get('sal_factor2'))
            sal_pack_unit = to_float(product.get('sal_pack_unit'))
            if boxes <= 0 and qty > 0 and sal_factor > 0:
                boxes = qty / sal_factor
            if ltrs <= 0 and qty > 0 and sal_pack_unit > 0:
                ltrs = qty * sal_pack_unit

        key = (state, variety, item_code, item_name, category)
        state_item_counts[key]['total_sales'] += float(item['total'] or 0)
        state_item_counts[key]['quantity'] += qty
        state_item_counts[key]['boxes'] += boxes
        state_item_counts[key]['ltrs'] += ltrs
        state_item_counts[key]['count'] += 1

    products_by_state = defaultdict(list)
    for (state, variety, item_code, item_name, category), values in state_item_counts.items():
        products_by_state[state].append({
            'variety': variety,
            'item_code': item_code,
            'item_name': item_name,
            'category': category,
            'total_sales': values['total_sales'],
            'quantity': values['quantity'],
            'boxes': values['boxes'],
            'ltrs': values['ltrs'],
            'count': values['count'],
        })

    state_rows = []
    for state, products in products_by_state.items():
        sorted_products = sorted(
            products,
            key=lambda x: (x['total_sales'], x['quantity'], x['count']),
            reverse=True
        )
        state_rows.append({
            'state': state,
            'products': sorted_products,
        })

    return sorted(
        state_rows,
        key=lambda x: sum(product['total_sales'] for product in x['products']),
        reverse=True
    )
    

class WDashboardKPIView(APIView):
    """The web Sales Dashboard's KPI numbers.

    Behind `Sales_Dashboard` since the page split: `/Dashboard` was open to
    every signed-in user only because it doubled as the landing page, and
    `/Home` taking that job is what let the analytics carry a gate. The web
    client is the only caller (`pages/dashboard/useDashboard.ts`).
    """

    def get_permissions(self):
        return [IsAuthenticated(), HasKey('Sales_Dashboard')]

    def get(self, request):
        today = timezone.now().date()
        now = timezone.now()
        year = int(request.query_params.get('year', now.year))
        month = int(request.query_params.get('month', 0))

        if month == 0:
            range_start = timezone.make_aware(datetime(year, 1, 1))
            if year == now.year:
                range_end = timezone.make_aware(
                    datetime(now.year, now.month, now.day, 23, 59, 59)
                )
            else:
                range_end = timezone.make_aware(
                    datetime(year, 12, 31, 23, 59, 59)
                )
        else:
            range_start = timezone.make_aware(datetime(year, month, 1))
            last_day = calendar.monthrange(year, month)[1]
            range_end = timezone.make_aware(
                datetime(year, month, last_day, 23, 59, 59)
            )

        base_order_ids = _get_base_orders(request.user).values_list('id', flat=True).distinct()
        all_orders = Order.objects.filter(id__in=base_order_ids)
        period_orders = all_orders.filter(
            created_at__gte=range_start,
            created_at__lte=range_end,
        )

        if month != 0:
            today_orders = (
                period_orders.filter(created_at__date=today).count()
                if year == now.year and month == now.month
                else 0
            )
            this_month_orders = period_orders.count()
        elif year == now.year:
            today_orders = period_orders.filter(created_at__date=today).count()
            current_month_start = today.replace(day=1)
            this_month_orders = period_orders.filter(
                created_at__date__gte=current_month_start
            ).count()
        else:
            month_start = timezone.make_aware(datetime(year, now.month, 1))
            last_day = calendar.monthrange(year, now.month)[1]
            month_end = timezone.make_aware(datetime(year, now.month, last_day, 23, 59, 59))
            today_orders = 0
            this_month_orders = period_orders.filter(
                created_at__gte=month_start,
                created_at__lte=month_end,
            ).count()

        total_orders = period_orders.count()
        total_revenue = period_orders.aggregate(total=Sum('total_amount'))['total'] or 0
        completed_revenue = period_orders.filter(
            Q(status__code__icontains='COMPLETED') |
            Q(status__name__icontains='Completed')
        ).aggregate(total=Sum('total_amount'))['total'] or 0
        rejected_revenue = period_orders.filter(
            Q(status__code__icontains='REJECTED') |
            Q(status__name__icontains='Rejected')
        ).aggregate(total=Sum('total_amount'))['total'] or 0
        pending_revenue = period_orders.exclude(
            Q(status__code__icontains='COMPLETED') |
            Q(status__name__icontains='Completed') |
            Q(status__code__icontains='REJECTED') |
            Q(status__name__icontains='Rejected')
        ).aggregate(total=Sum('total_amount'))['total'] or 0
        accepted_orders = 0
        rejected_orders = 0
        pending_review_orders = 0
        user_counts = {}

        role = getattr(request.user, 'role', None)
        role_name = getattr(role, 'name', '').lower() if role else ''
        # Keyed, not role-named — see `sees_all_orders`. A custom role granted
        # company-wide visibility gets the headcount tile too, instead of the
        # tile silently vanishing for everyone who is not literally 'admin'.
        if sees_all_orders(request.user):
            user_counts = {
                'manager': User.objects.filter(role__name__iexact='manager', is_active=True).count(),
                'auditor': User.objects.filter(role__name__iexact='auditor', is_active=True).count(),
                'billing': User.objects.filter(role__name__iexact='billing', is_active=True).count(),
            }
        if role_name == 'auditor':
            latest_auditor_action = (
                OrdersLog.objects
                .filter(
                    order=OuterRef('pk'),
                    performed_by=request.user,
                    action_id__in=AUDITOR_DECISION_ACTION_IDS,
                )
                .order_by('-created_at', '-id')
                .values('action_id')[:1]
            )
            auditor_orders_with_decision = period_orders.annotate(
                latest_auditor_action_id=Subquery(latest_auditor_action)
            )
            auditor_decided_orders = (
                auditor_orders_with_decision
                .filter(latest_auditor_action_id__in=AUDITOR_DECISION_ACTION_IDS)
                .exclude(status__code='AUDITOR_APPROVAL')
                .distinct()
            )

            pending_review_orders = (
                auditor_orders_with_decision
                .filter(status__code='AUDITOR_APPROVAL')
                .distinct()
                .count()
            )

            accepted_orders = auditor_decided_orders.filter(
                latest_auditor_action_id=AUDITOR_ACCEPTED_ACTION_ID
            ).count()

            rejected_orders = auditor_decided_orders.filter(
                latest_auditor_action_id=AUDITOR_REJECTED_ACTION_ID
            ).count()
        if role_name == 'billing':
            latest_billing_action = (
                OrdersLog.objects
                .filter(
                    order=OuterRef('pk'),
                    performed_by=request.user,
                    action_id__in=BILLING_DECISION_ACTION_IDS,
                )
                .order_by('-created_at', '-id')
                .values('action_id')[:1]
            )
            billing_orders_with_decision = period_orders.annotate(
                latest_billing_action_id=Subquery(latest_billing_action)
            )
            billing_decided_orders = (
                billing_orders_with_decision
                .filter(latest_billing_action_id__in=BILLING_DECISION_ACTION_IDS)
                .exclude(status__code__in=BILLING_ACTIVE_CODES)
                .distinct()
            )

            pending_review_orders = (
                billing_orders_with_decision
                .filter(status__code__in=BILLING_ACTIVE_CODES)
                .distinct()
                .count()
            )

            accepted_orders = billing_decided_orders.filter(
                latest_billing_action_id=BILLING_ACCEPTED_ACTION_ID
            ).count()

            rejected_orders = billing_decided_orders.filter(
                latest_billing_action_id=BILLING_REJECTED_ACTION_ID
            ).count()

        if role_name == 'approver':
            period_approvals = OrderRateApproval.objects.filter(
                approver=request.user,
                order__created_at__gte=range_start,
                order__created_at__lte=range_end,
            )

            pending_review_orders = period_orders.count()
            accepted_orders = period_approvals.filter(status='APPROVED').count()
            rejected_orders = period_approvals.filter(status='REJECTED').count()

            total_orders = accepted_orders + rejected_orders + pending_review_orders

        status_counts = {}
        for os in OrderStatus.objects.all():
            status_counts[os.name] = period_orders.filter(status=os).count()

        return Response({
            'filter': {'year': year, 'month': month},
            'total_orders': total_orders,
            'total_revenue': str(total_revenue),
            'completed_revenue': str(completed_revenue),
            'rejected_revenue': str(rejected_revenue),
            'pending_revenue': str(pending_revenue),
            'today_orders': today_orders,
            'this_month_orders': this_month_orders,
            'status_counts': status_counts,
            'user_counts': user_counts,
            'accepted_orders': accepted_orders,
            'rejected_orders': rejected_orders,
            'pending_review_orders': pending_review_orders,
            'reviewed_orders': accepted_orders + rejected_orders,
        })

class WDashboardChartsView(APIView):
    """Chart series for the Sales Dashboard — and for the State-Wise report.

    Two screens, two gates, one endpoint: `StateWise_Report.tsx` calls this
    same URL with a `status` filter, and that page sits under the `Reports`
    grant. `HasKey('Sales_Dashboard')` here would have silently emptied the
    State-Wise report for everyone holding `Reports` alone, so the gate names
    both keys explicitly rather than falling back to bare `IsAuthenticated`.
    """

    def get_permissions(self):
        return [IsAuthenticated(), HasAnyKey('Sales_Dashboard', 'Reports')]

    def get(self, request):
        now = timezone.now()
        # Donut charts filter
        year = int(request.query_params.get('year', now.year))
        month = int(request.query_params.get('month', 0))
        # Line chart filter (independent)
        line_year = int(request.query_params.get('line_year', now.year))

        # Date boundaries for donut charts: month=0 means year-to-date
        if month == 0:
            range_start = timezone.make_aware(datetime(year, 1, 1))
            if year == now.year:
                range_end = timezone.make_aware(datetime(now.year, now.month, now.day, 23, 59, 59))
            else:
                range_end = timezone.make_aware(datetime(year, 12, 31, 23, 59, 59))
        else:
            range_start = timezone.make_aware(datetime(year, month, 1))
            last_day = calendar.monthrange(year, month)[1]
            range_end = timezone.make_aware(datetime(year, month, last_day, 23, 59, 59))

        base_order_ids = _get_base_orders(request.user).values_list('id', flat=True).distinct()
        base_orders = Order.objects.filter(id__in=base_order_ids)
        filtered_orders = base_orders.filter(
            created_at__gte=range_start,
            created_at__lte=range_end
        )
        completed_orders = filtered_orders.filter(
            Q(status__code__icontains='COMPLETED') |
            Q(status__name__icontains='Completed')
        )
        role = getattr(request.user, 'role', None)
        role_name = getattr(role, 'name', '').lower() if role else ''

        # CHART 1: Monthly Sales Timeline (full line_year Jan-Dec)
        year_start = timezone.make_aware(datetime(line_year, 1, 1))
        year_end = timezone.make_aware(datetime(line_year, 12, 31, 23, 59, 59))
        monthly_sales = (
            base_orders
            .filter(created_at__gte=year_start, created_at__lte=year_end)
            .filter(
                Q(status__code__icontains='COMPLETED') |
                Q(status__name__icontains='Completed')
            )
            .annotate(month=TruncMonth('created_at'))
            .values('month')
            .annotate(revenue=Sum('total_amount'), count=Count('id', distinct=True))
            .order_by('month')
        )
        sales_map = {
            entry['month'].month: {
                'revenue': float(entry['revenue'] or 0),
                'count': entry['count'],
            }
            for entry in monthly_sales
        }
        monthly_sales_data = [
            {
                'month': f'{line_year}-{m:02d}',
                'label': calendar.month_abbr[m],
                'revenue': sales_map.get(m, {}).get('revenue', 0),
                'count': sales_map.get(m, {}).get('count', 0),
            }
            for m in range(1, 13)
        ]

        # "Orders Received" for an approver is every order routed to them, grouped by the
        # month the order was created. base_orders only holds the still-pending ones, so the
        # default completed-based count is empty for approvers. To stay consistent with the
        # KPI's total_orders (approved + rejected + pending-at-approver-stage), we count the
        # same set: approvals this approver decided, plus those still pending in their queue.
        if role_name == 'approver':
            approver_monthly = (
                OrderRateApproval.objects
                .filter(
                    approver=request.user,
                    order__created_at__gte=year_start,
                    order__created_at__lte=year_end,
                )
                .filter(
                    Q(status__in=['APPROVED', 'REJECTED']) |
                    Q(status='PENDING', order__status__code__in=APPROVER_ACTIVE_CODES)
                )
                .annotate(month=TruncMonth('order__created_at'))
                .values('month')
                .annotate(count=Count('order_id', distinct=True))
            )
            approver_month_map = {
                entry['month'].month: entry['count'] for entry in approver_monthly
            }
            for entry in monthly_sales_data:
                entry['count'] = approver_month_map.get(int(entry['month'].split('-')[1]), 0)

        # CHART 2: State-wise Orders (selected month)
        # No FK between Order and Parties; join via card_code in Python
        card_codes_in_month = list(
            completed_orders.values_list('card_code', flat=True).distinct()
        )
        state_map = _build_party_state_map(card_codes_in_month)

        state_counts = defaultdict(lambda: {'orders': 0, 'sales': 0})
        for order in completed_orders.values('card_code', 'total_amount'):
            state = state_map.get(str(order['card_code'] or '').strip(), 'Unknown')
            state_counts[state]['orders'] += 1
            state_counts[state]['sales'] += float(order['total_amount'] or 0)

        statewise_data = sorted(
            [
                {
                    'state': state,
                    'orders': values['orders'],
                    'sales': values['sales'],
                }
                for state, values in state_counts.items()
            ],
            key=lambda x: x['sales'],
            reverse=True
        )

        manager_performance = (
            completed_orders
            .filter(created_by__role__name__iexact='manager')
            .values('created_by_id', 'created_by__name', 'created_by__username')
            .annotate(sales=Sum('total_amount'), orders=Count('id', distinct=True))
            .order_by('-sales', '-orders')
        )
        manager_sales_map = {
            entry['created_by_id']: {
                'orders': entry['orders'],
                'sales': float(entry['sales'] or 0),
            }
            for entry in manager_performance
        }
        active_managers = User.objects.filter(
            role__name__iexact='manager',
            is_active=True,
        ).values('id', 'name', 'username')
        manager_performance_data = sorted(
            [
                {
                    'manager_id': manager['id'],
                    'manager_name': manager['name'] or manager['username'] or 'Unknown',
                    'orders': manager_sales_map.get(manager['id'], {}).get('orders', 0),
                    'sales': manager_sales_map.get(manager['id'], {}).get('sales', 0),
                }
                for manager in active_managers
            ],
            key=lambda x: (x['sales'], x['orders']),
            reverse=True
        )

        manager_state_counts = defaultdict(lambda: {'orders': 0, 'sales': 0})
        for order in completed_orders.filter(created_by__role__name__iexact='manager').values(
            'created_by_id',
            'created_by__name',
            'created_by__username',
            'card_code',
            'total_amount',
        ):
            state = state_map.get(str(order['card_code'] or '').strip(), 'Unknown')
            manager_name = order['created_by__name'] or order['created_by__username'] or 'Unknown'
            key = (order['created_by_id'], manager_name, state)
            manager_state_counts[key]['orders'] += 1
            manager_state_counts[key]['sales'] += float(order['total_amount'] or 0)

        manager_state_performance_data = sorted(
            [
                {
                    'manager_id': manager_id,
                    'manager_name': manager_name,
                    'state': state,
                    'orders': values['orders'],
                    'sales': values['sales'],
                }
                for (manager_id, manager_name, state), values in manager_state_counts.items()
            ],
            key=lambda x: x['sales'],
            reverse=True
        )

        # CHART 3: Status Distribution (selected month) - include all statuses
        status_counts_map = dict(
            filtered_orders
            .values('status')
            .annotate(count=Count('id', distinct=True))
            .values_list('status', 'count')
        )
        status_data = [
            {'status': os.code, 'label': os.name, 'count': status_counts_map.get(os.id, 0)}
            for os in OrderStatus.objects.all()
        ]

        decision_data = []
        if role_name == 'billing':
            latest_billing_action = (
                OrdersLog.objects
                .filter(
                    order=OuterRef('pk'),
                    performed_by=request.user,
                    action_id__in=BILLING_DECISION_ACTION_IDS,
                )
                .order_by('-created_at', '-id')
                .values('action_id')[:1]
            )
            billing_orders_with_decision = filtered_orders.annotate(
                latest_billing_action_id=Subquery(latest_billing_action)
            )
            billing_decided_orders = (
                billing_orders_with_decision
                .filter(latest_billing_action_id__in=BILLING_DECISION_ACTION_IDS)
                .exclude(status__code__in=BILLING_ACTIVE_CODES)
                .distinct()
            )
            decision_data = [
                {
                    'status': 'accepted',
                    'label': 'Accepted',
                    'count': billing_decided_orders.filter(
                        latest_billing_action_id=BILLING_ACCEPTED_ACTION_ID
                    ).count(),
                },
                {
                    'status': 'rejected',
                    'label': 'Rejected',
                    'count': billing_decided_orders.filter(
                        latest_billing_action_id=BILLING_REJECTED_ACTION_ID
                    ).count(),
                },
                {
                    'status': 'queue',
                    'label': 'Pending',
                    'count': billing_orders_with_decision.filter(
                        status__code__in=BILLING_ACTIVE_CODES
                    ).distinct().count(),
                },
            ]
        elif role_name == 'auditor':
            latest_auditor_action = (
                OrdersLog.objects
                .filter(
                    order=OuterRef('pk'),
                    performed_by=request.user,
                    action_id__in=AUDITOR_DECISION_ACTION_IDS,
                )
                .order_by('-created_at', '-id')
                .values('action_id')[:1]
            )
            auditor_orders_with_decision = filtered_orders.annotate(
                latest_auditor_action_id=Subquery(latest_auditor_action)
            )
            auditor_decided_orders = (
                auditor_orders_with_decision
                .filter(latest_auditor_action_id__in=AUDITOR_DECISION_ACTION_IDS)
                .exclude(status__code='AUDITOR_APPROVAL')
                .distinct()
            )
            decision_data = [
                {
                    'status': 'accepted',
                    'label': 'Accepted',
                    'count': auditor_decided_orders.filter(
                        latest_auditor_action_id=AUDITOR_ACCEPTED_ACTION_ID
                    ).count(),
                },
                {
                    'status': 'rejected',
                    'label': 'Rejected',
                    'count': auditor_decided_orders.filter(
                        latest_auditor_action_id=AUDITOR_REJECTED_ACTION_ID
                    ).count(),
                },
                {
                    'status': 'pending',
                    'label': 'Pending Review',
                    'count': auditor_orders_with_decision.filter(
                        status__code='AUDITOR_APPROVAL'
                    ).distinct().count(),
                },
            ]
        elif role_name == 'approver':
            approver_order_ids = OrderRateApproval.objects.filter(
                approver=request.user,
            ).values_list('order_id', flat=True).distinct()
            approver_period_orders = Order.objects.filter(
                id__in=approver_order_ids,
                created_at__gte=range_start,
                created_at__lte=range_end,
            )
            user_approvals = OrderRateApproval.objects.filter(
                order__in=approver_period_orders,
                approver=request.user,
            )
            decision_data = [
                {
                    'status': 'accepted',
                    'label': 'Approved',
                    'count': user_approvals.filter(status='APPROVED').count(),
                },
                {
                    'status': 'rejected',
                    'label': 'Rejected',
                    'count': user_approvals.filter(status='REJECTED').count(),
                },
                {
                    'status': 'pending',
                    'label': 'Pending Approval',
                    'count': filtered_orders.count(),
                },
            ]

        # CHART 4: Top Parties by category (selected period)
        # Revenue reflects only completed orders (scoped to the logged-in user via
        # _get_base_orders); count stays as the total orders in the period.
        completed_status_filter = (
            Q(order__status__code__icontains='COMPLETED')
            | Q(order__status__name__icontains='Completed')
        )
        top_parties = (
            OrderItem.objects
            .filter(order__in=filtered_orders)
            .values('order__card_code', 'category')
            .annotate(
                card_name=Max('order__card_name'),
                revenue=Sum('total', filter=completed_status_filter),
                count=Count('order_id', distinct=True),
                completed_count=Count(
                    'order_id',
                    distinct=True,
                    filter=completed_status_filter,
                ),
            )
            .order_by('-count', '-revenue')
        )
        top_parties_data = [
            {
                'card_code': entry['order__card_code'],
                'card_name': entry['card_name'],
                'category': entry['category'] or 'Unknown',
                'count': entry['count'],
                'completed_count': entry['completed_count'],
                'revenue': float(entry['revenue'] or 0),
            }
            for entry in top_parties
        ]

        # CHART 5: Category-wise Sales (selected month)
        category_order_scope = (completed_orders if sees_all_orders(request.user)
                                else filtered_orders)
        category_sales = (
            OrderItem.objects
            .filter(order__in=category_order_scope)
            .values('category')
            .annotate(total_sales=Sum('total'), count=Count('id', distinct=True))
            .order_by('-total_sales')
        )
        category_data = [
            {
                'category': entry['category'] or 'Unknown',
                'total_sales': float(entry['total_sales'] or 0),
                'count': entry['count'],
            }
            for entry in category_sales
        ]
        
        req_status = request.query_params.get('status', 'completed').lower()
        if req_status == 'pending':
            target_orders = filtered_orders.exclude(
                Q(status__code__icontains='COMPLETED') |
                Q(status__name__icontains='Completed') |
                Q(status__code__icontains='REJECTED') |
                Q(status__name__icontains='Rejected')
            )
        elif req_status == 'rejected':
            target_orders = filtered_orders.filter(
                Q(status__code__icontains='REJECTED') |
                Q(status__name__icontains='Rejected')
            )
        elif req_status == 'all':
            target_orders = filtered_orders
        else:
            target_orders = completed_orders
            
        target_card_codes = list(
            target_orders.values_list('card_code', flat=True).distinct()
        )
        state_map = _build_party_state_map(target_card_codes)
        state_item_sales = _build_state_item_sales(target_orders, state_map)
        highest_sales_order = (
            filtered_orders
            .order_by('-total_amount')
            .values('order_number', 'total_amount')
            .first()
        )

        return Response({
            'filter': {'year': year, 'month': month, 'line_year': line_year},
            'monthly_sales': monthly_sales_data,
            'statewise_orders': statewise_data,
            'manager_performance': manager_performance_data,
            'manager_state_performance': manager_state_performance_data,
            'status_distribution': status_data,
            'decision_distribution': decision_data,
            'top_parties': top_parties_data,
            'category_sales': category_data,
            'state_item_sales': state_item_sales,
            'highest_sales_order': {
                'order_number': highest_sales_order['order_number'] if highest_sales_order else None,
                'amount': float(highest_sales_order['total_amount'] or 0) if highest_sales_order else 0,
            },
        })

class DashboardKPIView(APIView):

    def get(self, request):
        from django.contrib.auth import get_user_model
        from users.models import UserRole

        today = timezone.now().date()
        current_month_start = today.replace(day=1)

        all_orders = _get_base_orders(request.user)

        total_orders = all_orders.count()
        total_revenue = all_orders.aggregate(total=Sum('total_amount'))['total'] or 0
        today_orders = all_orders.filter(created_at__date=today).count()
        this_month_orders = all_orders.filter(created_at__date__gte=current_month_start).count()

        status_counts = {}
        for os in OrderStatus.objects.all():
            status_counts[os.name] = all_orders.filter(status=os).count()

        User = get_user_model()
        user_counts = {}
        for role in UserRole.objects.filter(is_active=True):
            user_counts[role.name] = User.objects.filter(role=role, is_active=True).count()

        return Response({
            'total_orders': total_orders,
            'total_revenue': str(total_revenue),
            'today_orders': today_orders,
            'this_month_orders': this_month_orders,
            'status_counts': status_counts,
            'user_counts': user_counts,
        })

class DashboardChartsView(APIView):

    def get(self, request):
        now = timezone.now()
        # Donut charts filter
        year = int(request.query_params.get('year', now.year))
        month = int(request.query_params.get('month', 0))
        # Line chart filter (independent)
        line_year = int(request.query_params.get('line_year', now.year))

        # Date boundaries for donut charts: month=0 means year-to-date
        if month == 0:
            range_start = timezone.make_aware(datetime(year, 1, 1))
            if year == now.year:
                range_end = timezone.make_aware(datetime(now.year, now.month, now.day, 23, 59, 59))
            else:
                range_end = timezone.make_aware(datetime(year, 12, 31, 23, 59, 59))
        else:
            range_start = timezone.make_aware(datetime(year, month, 1))
            last_day = calendar.monthrange(year, month)[1]
            range_end = timezone.make_aware(datetime(year, month, last_day, 23, 59, 59))

        base_orders = _get_base_orders(request.user)
        filtered_orders = base_orders.filter(
            created_at__gte=range_start,
            created_at__lte=range_end
        )
        completed_orders = filtered_orders.filter(
            Q(status__code__icontains='COMPLETED') |
            Q(status__name__icontains='Completed')
        )

        year_start = timezone.make_aware(datetime(line_year, 1, 1))
        year_end = timezone.make_aware(datetime(line_year, 12, 31, 23, 59, 59))
        monthly_sales = (
            base_orders
            .filter(created_at__gte=year_start, created_at__lte=year_end)
            .filter(
                Q(status__code__icontains='COMPLETED') |
                Q(status__name__icontains='Completed')
            )
            .annotate(month=TruncMonth('created_at'))
            .values('month')
            .annotate(revenue=Sum('total_amount'), count=Count('id'))
            .order_by('month')
        )
        sales_map = {
            entry['month'].month: {
                'revenue': float(entry['revenue'] or 0),
                'count': entry['count'],
            }
            for entry in monthly_sales
        }
        monthly_sales_data = [
            {
                'month': f'{line_year}-{m:02d}',
                'label': calendar.month_abbr[m],
                'revenue': sales_map.get(m, {}).get('revenue', 0),
                'count': sales_map.get(m, {}).get('count', 0),
            }
            for m in range(1, 13)
        ]

        # CHART 2: State-wise Orders (selected month)
        # No FK between Order and Parties; join via card_code in Python
        card_codes_in_month = list(
            filtered_orders.values_list('card_code', flat=True).distinct()
        )
        state_map = _build_party_state_map(card_codes_in_month)

        state_counts = defaultdict(int)
        for order in filtered_orders.values('card_code'):
            state = state_map.get(str(order['card_code'] or '').strip(), 'Unknown')
            state_counts[state] += 1

        statewise_data = sorted(
            [{'state': k, 'orders': v} for k, v in state_counts.items()],
            key=lambda x: x['orders'],
            reverse=True
        )

        # CHART 3: Status Distribution (selected month) - include all statuses
        status_counts_map = dict(
            filtered_orders
            .values('status')
            .annotate(count=Count('id'))
            .values_list('status', 'count')
        )
        status_data = [
            {'status': os.code, 'label': os.name, 'count': status_counts_map.get(os.id, 0)}
            for os in OrderStatus.objects.all()
        ]

        # CHART 4: Top Parties by category (selected period)
        top_parties = (
            OrderItem.objects
            .filter(order__in=filtered_orders)
            .values('order__card_code', 'category')
            .annotate(
                card_name=Max('order__card_name'),
                revenue=Sum('total'),
                count=Count('order_id', distinct=True),
                completed_count=Count(
                    'order_id',
                    distinct=True,
                    filter=Q(order__status__code__icontains='COMPLETED') | Q(order__status__name__icontains='Completed'),
                ),
            )
            .order_by('-count', '-revenue')
        )
        top_parties_data = [
            {
                'card_code': entry['order__card_code'],
                'card_name': entry['card_name'],
                'category': entry['category'] or 'Unknown',
                'count': entry['count'],
                'completed_count': entry['completed_count'],
                'revenue': float(entry['revenue'] or 0),
            }
            for entry in top_parties
        ]

        # CHART 5: Category-wise Sales (selected month)
        category_sales = (
            OrderItem.objects
            .filter(order__in=filtered_orders)
            .values('category')
            .annotate(total_sales=Sum('total'), count=Count('id'))
            .order_by('-total_sales')
        )
        category_data = [
            {
                'category': entry['category'] or 'Unknown',
                'total_sales': float(entry['total_sales'] or 0),
                'count': entry['count'],
            }
            for entry in category_sales
        ]
        
        req_status = request.query_params.get('status', 'completed').lower()
        if req_status == 'pending':
            target_orders = filtered_orders.exclude(
                Q(status__code__icontains='COMPLETED') |
                Q(status__name__icontains='Completed') |
                Q(status__code__icontains='REJECTED') |
                Q(status__name__icontains='Rejected')
            )
        elif req_status == 'rejected':
            target_orders = filtered_orders.filter(
                Q(status__code__icontains='REJECTED') |
                Q(status__name__icontains='Rejected')
            )
        elif req_status == 'all':
            target_orders = filtered_orders
        else:
            target_orders = completed_orders

        state_item_sales = _build_state_item_sales(target_orders, state_map)

        return Response({
            'filter': {'year': year, 'month': month, 'line_year': line_year},
            'monthly_sales': monthly_sales_data,
            'statewise_orders': statewise_data,
            'status_distribution': status_data,
            'top_parties': top_parties_data,
            'category_sales': category_data,
            'state_item_sales': state_item_sales,
        })
