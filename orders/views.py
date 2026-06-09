from urllib import request
from django.shortcuts import render
import re
from .serializers import SchemeProductSerializer,OrderDetailSerializer, OrderListByUserIdSerializer,OrdersLogSerializer,OrderStatusUpdateSerializer, DispatchLocationSerializer,BranchSerializer, PartyAddressSerializer,ProductSerializer,CreateOrderSerializer,OrderItemSerializer, CreateSchemeSerializer,OrderItemSchemeSerializer, NotificationSerializer,StaffProductSerializer
from .models import PartyProductAssignment,OrdersLog,Parties, Branches, DispatchLocation, UserPartyAssignment, PartyAddress,ProductDetails,Order,OrderItem,OrderStatus,log_order_action, OrderItemScheme,OrderItemScheme,Template, Notification, PushToken, StaffProductPrice, OrderFlowConfig, RateApproverRule,OrderRateApproval,OrderItemApprovalMapping
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from datetime import datetime
from functools import lru_cache
from rest_framework.permissions import IsAdminUser
import calendar
from django.db.models import Sum, Count,F,Q, OuterRef, Subquery
from django.db.models.functions import TruncMonth
from django.utils import timezone
from collections import defaultdict
from django.shortcuts import get_object_or_404
from rest_framework import permissions
from sap_sync.models import Party as SapParty, PartyAddress as SapPartyAddress, Product as SapProduct, active_product_q
from sap_sync.services.connection import SAPConnection
from .models import Order, OrderStatus
from .models import PartyProductAssignment
from .scheme_rules import (
    get_ordered_quantity,
    get_party_product_scheme,
    should_mirror_punjab_combo_scheme_qty)
from users.models import SchemeProduct, User, State
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
import json
import requests
from .ai_service import get_order_summary


def _active_sap_item_codes():
    return SapProduct.objects.filter(active_product_q()).values_list('item_code', flat=True)


BILLING_ACTIVE_CODES = ['BILLING', 'BILLING_PENDING']
BILLING_RESOLVED_CODES = ['BILLING_REJECTED', 'COMPLETED']
BILLING_ACCEPTED_ACTION_ID = 3
BILLING_REJECTED_ACTION_ID = 8
BILLING_DECISION_ACTION_IDS = [BILLING_ACCEPTED_ACTION_ID, BILLING_REJECTED_ACTION_ID]
AUDITOR_ACCEPTED_ACTION_ID = 9
AUDITOR_REJECTED_ACTION_ID = 7
AUDITOR_DECISION_ACTION_IDS = [AUDITOR_ACCEPTED_ACTION_ID, AUDITOR_REJECTED_ACTION_ID]
APPROVER_ACCEPTED_ACTION_ID = 6
APPROVER_REJECTED_ACTION_ID = 7
APPROVER_DECISION_ACTION_IDS = [APPROVER_ACCEPTED_ACTION_ID, APPROVER_REJECTED_ACTION_ID]
APPROVER_ACTIVE_CODES = ['NEED_APPROVAL', 'RATE_APPROVAL']
RATE_CONDITION_CHOICES = {
    'BASIC_GT_MARKET': 'Basic Price > Market Price and Market Price != 0',
    'BASIC_LT_MARKET': 'Basic Price < Market Price',
    'BASIC_EQ_MARKET': 'Basic Price = Market Price',
    'BASIC_MARKET_ZERO': 'Basic Price and Market Price = 0',
    'BASIC_ZERO_MARKET_GT_ZERO': 'Basic Price = 0 and Market Price > 0',
}
DEFAULT_RATE_CONDITIONS = ['BASIC_GT_MARKET']
ORDER_FLOW_TYPE_ASM = 'ASM'
ORDER_FLOW_TYPE_BILLING = 'BILLING'
ORDER_FLOW_TYPE_CHOICES = {
    ORDER_FLOW_TYPE_ASM: 'ASM Order Flow',
    ORDER_FLOW_TYPE_BILLING: 'Billing Orders Flow',
}

@csrf_exempt
def ai_order_summary(request):
    data = json.loads(request.body)

    result = get_order_summary(data)

    return JsonResponse({"summary": result})

def _normalize_order_flow_type(flow_type):
    flow_type = str(flow_type or ORDER_FLOW_TYPE_ASM).strip().upper()
    return flow_type if flow_type in ORDER_FLOW_TYPE_CHOICES else ORDER_FLOW_TYPE_ASM

def _default_order_flow_values(flow_type):
    if flow_type == ORDER_FLOW_TYPE_BILLING:
        return {
            'rate_approval_enabled': False,
            'billing_enabled': False,
            'auditor_enabled': True,
            'rate_conditions': [],
        }
    return {
        'rate_approval_enabled': True,
        'billing_enabled': True,
        'auditor_enabled': True,
        'rate_conditions': DEFAULT_RATE_CONDITIONS,
    }

def _get_order_flow_config(flow_type=ORDER_FLOW_TYPE_ASM):
    flow_type = _normalize_order_flow_type(flow_type)
    config, _created = OrderFlowConfig.objects.get_or_create(
        flow_type=flow_type,
        defaults=_default_order_flow_values(flow_type),
    )
    if not isinstance(config.rate_conditions, list):
        config.rate_conditions = _default_order_flow_values(flow_type)['rate_conditions']
    return config

def _order_flow_config_payload(config=None, flow_type=ORDER_FLOW_TYPE_ASM):
    config = config or _get_order_flow_config(flow_type)
    rate_conditions = [
        condition
        for condition in (config.rate_conditions or [])
        if condition in RATE_CONDITION_CHOICES
    ]
    return {
        'flow_type': config.flow_type,
        'flow_label': ORDER_FLOW_TYPE_CHOICES.get(config.flow_type, config.flow_type),
        'flow_options': [
            {'code': code, 'label': label}
            for code, label in ORDER_FLOW_TYPE_CHOICES.items()
        ],
        'rate_approval_enabled': bool(config.rate_approval_enabled),
        'billing_enabled': bool(config.billing_enabled),
        'auditor_enabled': bool(config.auditor_enabled),
        'rate_conditions': rate_conditions,
        'condition_options': [
            {'code': code, 'label': label}
            for code, label in RATE_CONDITION_CHOICES.items()
        ],
        'updated_at': config.updated_at.isoformat() if config.updated_at else None,
        'updated_by': getattr(config.updated_by, 'username', None),
    }

def _get_price_condition_code(basic_price, market_price):
    if basic_price == 0 and market_price == 0:
        return 'BASIC_MARKET_ZERO'
    if basic_price > market_price:
        return 'BASIC_GT_MARKET'
    if basic_price < market_price:
        return 'BASIC_LT_MARKET'
    return 'BASIC_EQ_MARKET'

def _item_can_match_flow_condition(item):
    if item.get('item_type') == 'SCHEME':
        return False
    try:
        return float(item.get('qty') or 0) > 0
    except (TypeError, ValueError):
        return False

def _get_order_price_condition_codes(items, to_float):
    codes = set()
    for item in items:
        if not _item_can_match_flow_condition(item):
            continue
        bp = to_float(item.get('basic_price', 0))
        mp = to_float(item.get('market_price', 0))
        if bp == 0 and mp == 0:
            codes.add('BASIC_EQ_MARKET')
        if bp == 0 and mp > 0:
            codes.add('BASIC_ZERO_MARKET_GT_ZERO')
            continue
        if bp > 0 and mp == 0:
            continue
        codes.add(_get_price_condition_code(bp, mp))
    return codes

def _get_status_by_name(name):
    return (
        OrderStatus.objects.filter(name__iexact=name).first()
        or OrderStatus.objects.filter(code__iexact=str(name).replace(' ', '_')).first()
    )

def _get_configured_stage_statuses(config=None, flow_type=ORDER_FLOW_TYPE_ASM):
    config = config or _get_order_flow_config(flow_type)
    stages = []
    if config.rate_approval_enabled:
        stages.append('Rate Approval')
    if config.billing_enabled:
        stages.append('Billing')
    if config.auditor_enabled:
        stages.append('Auditor Approval')
    stages.append('Completed')
    return [status for status in (_get_status_by_name(name) for name in stages) if status]

def _get_foc_stage_statuses():
    return [
        status
        for status in (
            _get_status_by_name('Billing'),
            _get_status_by_name('Auditor Approval'),
            _get_status_by_name('Completed'),
        )
        if status
    ]

def _get_next_foc_status(current_status, fallback_name='Billing'):
    if not current_status:
        return _get_status_by_name(fallback_name)

    stages = _get_foc_stage_statuses()
    current_id = getattr(current_status, 'id', None)
    for index, status_obj in enumerate(stages):
        if status_obj.id == current_id:
            if index + 1 < len(stages):
                return stages[index + 1]
            return status_obj

    return _get_status_by_name(fallback_name)

def _get_initial_flow_status(items, to_float, fallback_name='Billing', flow_type=ORDER_FLOW_TYPE_ASM, force_foc_flow=False):
    if force_foc_flow:
        return _get_status_by_name('Billing'), False

    config = _get_order_flow_config(flow_type)
    condition_codes = _get_order_price_condition_codes(items, to_float)
    selected_conditions = set(config.rate_conditions or [])

    if (
        config.rate_approval_enabled
        and selected_conditions
        and condition_codes.intersection(selected_conditions)
    ):
        rate_status = _get_status_by_name('Rate Approval')
        if rate_status:
            return rate_status, True

    for status_name, enabled in (
        ('Billing', config.billing_enabled),
        ('Auditor Approval', config.auditor_enabled),
        ('Completed', True),
    ):
        if enabled:
            status_obj = _get_status_by_name(status_name)
            if status_obj:
                return status_obj, False

    return _get_status_by_name(fallback_name), False

def _get_next_order_flow_status(order, current_status, fallback_name=None, flow_type=ORDER_FLOW_TYPE_ASM):
    if getattr(order, 'is_foc', False):
        return _get_next_foc_status(current_status, fallback_name or 'Billing')
    return _get_next_flow_status(current_status, fallback_name, flow_type=flow_type)

def _get_next_flow_status(current_status, fallback_name=None, flow_type=ORDER_FLOW_TYPE_ASM):
    if not current_status:
        return _get_status_by_name(fallback_name) if fallback_name else None

    configured_statuses = _get_configured_stage_statuses(flow_type=flow_type)
    current_id = getattr(current_status, 'id', None)
    for index, status_obj in enumerate(configured_statuses):
        if status_obj.id == current_id:
            if index + 1 < len(configured_statuses):
                return configured_statuses[index + 1]
            return status_obj

    return _get_status_by_name(fallback_name) if fallback_name else None

def _get_order_flow_type_for_order(order):
    role_name = getattr(getattr(getattr(order, 'created_by', None), 'role', None), 'name', '')
    return ORDER_FLOW_TYPE_BILLING if str(role_name).strip().lower() == 'billing' else ORDER_FLOW_TYPE_ASM

def _is_rejection_status(status_obj):
    text = f"{getattr(status_obj, 'code', '')} {getattr(status_obj, 'name', '')}".lower()
    return 'reject' in text

def _is_completed_status(status_obj):
    text = f"{getattr(status_obj, 'code', '')} {getattr(status_obj, 'name', '')}".lower()
    return 'completed' in text

def _order_status_message(status_obj, default_action='updated'):
    status_name = getattr(status_obj, 'name', None) or str(status_obj or '').strip() or 'next stage'
    status_text = status_name.strip().lower()
    if 'completed' in status_text:
        return 'Order completed successfully'
    return f"Order {default_action} and sent to {status_name}"

def _pending_log_user_for_status(status_obj, actor_user=None):
    return actor_user if _is_completed_status(status_obj) else None

def _rate_approval_remarks(flagged_items):
    return '; '.join(flagged_items) or 'Rate approval required by admin price condition'

def _get_rate_approval_reason(item, basic_price, market_price):
    if item.get('item_type') == 'SCHEME':
        return None
    
    if item.get('qty') not in (None, ''):
        qty = float(item.get('qty') or 0)
        if qty <= 0:
            return None

    item_name = item.get('item_name') or item.get('item_code') or 'Item'

    # if basic_price == 0:
    #     return f"{item_name}: Basic price is 0 (Market ₹{market_price})"

    if market_price > 0 and market_price < basic_price:
        return f"{item_name}: Market ₹{market_price} < Basic ₹{basic_price}"

    return None

def _get_rate_approval_reason(item, basic_price, market_price):
    if item.get('item_type') == 'SCHEME':
        return None

    if item.get('qty') not in (None, ''):
        try:
            qty = float(item.get('qty') or 0)
        except (TypeError, ValueError):
            return None
        if qty <= 0:
            return None

    item_name = item.get('item_name') or item.get('item_code') or 'Item'

    if basic_price == 0 and market_price == 0:
        return f"{item_name}: Basic Rs {basic_price} = Market Rs {market_price}"
    if basic_price == 0 and market_price > 0:
        return f"{item_name}: Basic Rs {basic_price} < Market Rs {market_price}"
    if basic_price > market_price:
        return f"{item_name}: Basic Rs {basic_price} > Market Rs {market_price}"
    if basic_price < market_price:
        return f"{item_name}: Basic Rs {basic_price} < Market Rs {market_price}"
    return None

def _resolve_scheme_by_id(scheme_id):
    if scheme_id:
        try:
            return SchemeProduct.objects.get(scheme_id=int(scheme_id))
        except (SchemeProduct.DoesNotExist, ValueError, TypeError):
            return None
    return None

def _extract_order_item_schemes(item, to_float):
    raw_schemes = item.get('schemes')
    if isinstance(raw_schemes, list):
        extracted = []
        for raw_scheme in raw_schemes:
            if not isinstance(raw_scheme, dict):
                continue
            scheme_obj = _resolve_scheme_by_id(raw_scheme.get('scheme_id') or raw_scheme.get('scheme'))
            scheme_qty = to_float(raw_scheme.get('scheme_qty', raw_scheme.get('qty_scheme', 0)))
            if scheme_obj and scheme_qty > 0:
                extracted.append((scheme_obj, scheme_qty))
        return extracted

    scheme_obj = _resolve_scheme_by_id(item.get('scheme_id') or item.get('scheme'))
    scheme_qty = to_float(item.get('scheme_qty', item.get('qty_scheme', 0)))
    return [(scheme_obj, scheme_qty)] if scheme_obj and scheme_qty > 0 else []

def _create_order_item(order, item, to_float, to_bool):
    item_schemes = _extract_order_item_schemes(item, to_float)
    first_scheme = item_schemes[0][0] if item_schemes else None
    total_scheme_qty = sum(qty for _, qty in item_schemes)

    order_item = OrderItem.objects.create(
        order=order,
        item_code=item.get('item_code', ''),
        item_name=item.get('item_name', ''),
        category=item.get('category', ''),
        brand=item.get('brand', ''),
        variety=item.get('variety', ''),
        item_type=item.get('item_type', ''),
        qty=to_float(item.get('qty', 0)),
        pcs=to_float(item.get('pcs', 0)),
        boxes=to_float(item.get('boxes', 0)),
        ltrs=to_float(item.get('ltrs', 0)),
        basic_price=to_float(item.get('basic_price', 0)),
        market_price=to_float(item.get('market_price', 0)),
        total=to_float(item.get('total', 0)),
        tax_rate=to_float(item.get('tax_rate', 0)),
        scheme=first_scheme,
        qty_scheme=total_scheme_qty,
        is_scheme_visible=to_bool(item.get('is_scheme_visible')) or bool(item_schemes),
    )

    OrderItemScheme.objects.bulk_create([
        OrderItemScheme(order_item=order_item, scheme=scheme_obj, qty_scheme=scheme_qty)
        for scheme_obj, scheme_qty in item_schemes
    ])

    return order_item


def _normalize_order_type(value):
    order_type = str(value or 'PARTY').strip().upper()
    return 'STAFF' if order_type == 'STAFF' else 'PARTY'


def _normalize_template_number(value):
    try:
        if value in (None, ''):
            return 0.0
        return round(float(value), 4)
    except (TypeError, ValueError):
        return 0.0


def _build_order_template_signature(order):
    item_signatures = []

    for item in order.items.all().prefetch_related('schemes').order_by('id'):
        scheme_signatures = sorted(
            (
                getattr(item_scheme, 'scheme_id', None),
                _normalize_template_number(getattr(item_scheme, 'qty_scheme', 0)),
            )
            for item_scheme in item.schemes.all()
            if getattr(item_scheme, 'scheme_id', None)
        )

        # Fallback for older rows that only use the single scheme fields.
        if not scheme_signatures and getattr(item, 'scheme_id', None):
            scheme_signatures = [(
                getattr(item, 'scheme_id', None),
                _normalize_template_number(getattr(item, 'qty_scheme', 0)),
            )]

        item_signatures.append((
            item.item_code or '',
            item.item_type or '',
            _normalize_template_number(item.qty),
            _normalize_template_number(item.pcs),
            _normalize_template_number(item.boxes),
            _normalize_template_number(item.ltrs),
            _normalize_template_number(item.basic_price),
            _normalize_template_number(item.market_price),
            tuple(scheme_signatures),
        ))

    return tuple(sorted(item_signatures))


def _has_duplicate_template(user, order):
    order_signature = _build_order_template_signature(order)
    existing_templates = (
        Template.objects.filter(user=user, order__card_code=order.card_code)
        .exclude(order=order)
        .select_related('order')
        .prefetch_related('order__items__schemes')
    )

    for template in existing_templates:
        if _build_order_template_signature(template.order) == order_signature:
            return True

    return False


def _save_template_if_unique(user, order):
    if not user:
        return

    if _has_duplicate_template(user, order):
        return

    Template.objects.get_or_create(user=user, order=order)


def _get_user_category_name(user):
    category_obj = getattr(user, 'category', None)
    category_name = getattr(category_obj, 'category', category_obj)
    normalized_category = str(category_name or '').strip().upper()
    return normalized_category or None


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
        'variety',
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
        variety = item['variety'] or 'Unknown'
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


def _build_iexact_filter(field_name, values):
    query = Q()
    for value in values:
        query |= Q(**{f'{field_name}__iexact': value})
    return query


def _apply_billing_order_scope(queryset, user):
    user_category = _get_user_category_name(user)
    user_main_groups = _get_user_main_group_names(user)

    if not user_category and not user_main_groups:
        return queryset

    party_queryset = SapParty.objects.all()
    if user_category:
        party_queryset = party_queryset.filter(category__iexact=user_category)
    if user_main_groups:
        party_queryset = party_queryset.filter(
            _build_iexact_filter('main_group', user_main_groups)
        )

    queryset = queryset.filter(
        card_code__in=party_queryset.values_list('card_code', flat=True)
    )

    if user_category:
        queryset = queryset.filter(items__category__iexact=user_category)

    return queryset.distinct()


def _billing_users_for_order(order, exclude_user=None):
    users = User.objects.filter(role__name__iexact='billing', is_active=True)
    if exclude_user:
        users = users.exclude(id=exclude_user.id)

    order_categories = {
        str(category or '').strip().upper()
        for category in order.items.exclude(category__isnull=True)
        .exclude(category='')
        .values_list('category', flat=True)
    }

    matching_users = []
    for user in users:
        user_category = _get_user_category_name(user)
        if user_category and user_category not in order_categories:
            continue

        user_main_groups = _get_user_main_group_names(user)
        if user_main_groups:
            party_match = SapParty.objects.filter(
                card_code=order.card_code,
            )
            if user_category:
                party_match = party_match.filter(category__iexact=user_category)
            party_match = party_match.filter(
                _build_iexact_filter('main_group', user_main_groups)
            )
            if not party_match.exists():
                continue

        matching_users.append(user)

    return matching_users

def _get_base_orders(user):
    """Scope orders by user role:
    - admin: all orders
    - manager: only orders created by this user
    - auditor: orders currently in or previously routed through auditor review
    - approver: orders pending approval (NEED_APPROVAL, RATE_APPROVAL)
    - billing: orders currently in billing or already handled by this billing user
    """
    role = getattr(user, 'role', None)
    role_name = getattr(role, 'name', '').lower() if role else ''
    if role_name == 'admin':
        return Order.objects.all()
    if role_name == 'manager':
        return Order.objects.filter(created_by=user.id)
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

def _assigned_rate_approvers_for_order(order, status_filter=None, exclude_user=None):
    approvals = order.rate_approvals.select_related('approver').all()
    if status_filter:
        approvals = approvals.filter(status=status_filter)
    if exclude_user:
        approvals = approvals.exclude(approver=exclude_user)
    return [approval.approver for approval in approvals if approval.approver]

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

def _get_rate_approvers_for_item(item):
    category = str(getattr(item, 'category', '') or '').strip()
    variety = str(getattr(item, 'variety', '') or '').strip()
    if not category:
        return []

    rule_query = RateApproverRule.objects.filter(
        category__iexact=category,
        is_active=True,
    )
    if variety:
        rule = rule_query.filter(variety__iexact=variety).select_related('approver').first()
        if not rule:
            rule = (
                rule_query
                .filter(Q(variety__isnull=True) | Q(variety=''))
                .select_related('approver')
                .first()
            )
    else:
        rule = (
            rule_query
            .filter(Q(variety__isnull=True) | Q(variety=''))
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
        user_varieties = [
            value.strip().lower()
            for value in str(getattr(user, 'variety', '') or '').split(',')
            if value.strip()
        ]
        if not user_varieties:
            matched_users.append(user)
            continue
        if variety and variety.lower() in user_varieties:
            matched_users.append(user)

    return matched_users

def assign_rate_approvers(order):
    """
    Create OrderRateApproval and OrderItemApprovalMapping
    based on item variety.
    """
    OrderRateApproval.objects.filter(order=order).delete()
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

    for approver in approvers:
        OrderRateApproval.objects.get_or_create(
            order=order,
            approver=approver,
            defaults={
                "status": "PENDING"
            }
        )
    

class WDashboardKPIView(APIView):
    permission_classes = [IsAuthenticated]

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
        if role_name == 'admin':
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
            user_approvals = OrderRateApproval.objects.filter(
                order__in=period_orders,
                approver=request.user,
            )

            accepted_orders = user_approvals.filter(status='APPROVED').count()
            rejected_orders = user_approvals.filter(status='REJECTED').count()
            pending_review_orders = user_approvals.filter(status='PENDING').count()

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
    permission_classes = [IsAuthenticated]

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
            user_approvals = OrderRateApproval.objects.filter(
                order__in=filtered_orders,
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
                    'count': user_approvals.filter(status='PENDING').count(),
                },
            ]

        # CHART 4: Top Parties by Revenue (selected period)
        top_parties = (
            filtered_orders
            .values('card_code', 'card_name')
            .annotate(revenue=Sum('total_amount'), count=Count('id', distinct=True))
            .order_by('-count', '-revenue')
        )
        top_parties_data = [
            {
                'card_code': entry['card_code'],
                'card_name': entry['card_name'],
                'count': entry['count'],
                'revenue': float(entry['revenue'] or 0),
            }
            for entry in top_parties
        ]

        # CHART 5: Category-wise Sales (selected month)
        category_order_scope = completed_orders if role_name == 'admin' else filtered_orders
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

class OrderStatusTrackingView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        mode = (request.query_params.get('mode') or '').strip().lower()
        base_orders = _get_base_orders(request.user).select_related('status', 'created_by')

        if mode == 'auditor':
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
            orders = base_orders.annotate(
                latest_decision_action_id=Subquery(latest_auditor_action)
            )
            accepted_orders = orders.filter(
                latest_decision_action_id=AUDITOR_ACCEPTED_ACTION_ID
            ).exclude(status__code='AUDITOR_APPROVAL')
            rejected_orders = orders.filter(
                latest_decision_action_id=AUDITOR_REJECTED_ACTION_ID
            ).exclude(status__code='AUDITOR_APPROVAL')
        elif mode == 'billing':
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
            orders = base_orders.annotate(
                latest_decision_action_id=Subquery(latest_billing_action)
            )
            accepted_orders = orders.filter(
                latest_decision_action_id=BILLING_ACCEPTED_ACTION_ID
            ).exclude(status__code__in=BILLING_ACTIVE_CODES)
            rejected_orders = orders.filter(
                latest_decision_action_id=BILLING_REJECTED_ACTION_ID
            ).exclude(status__code__in=BILLING_ACTIVE_CODES)
        elif mode in ['rate_approver', 'rate approver', 'approver']:
            accepted_orders = base_orders.filter(
                rate_approvals__approver=request.user,
                rate_approvals__status='APPROVED',
            )
            rejected_orders = base_orders.filter(
                rate_approvals__approver=request.user,
                rate_approvals__status='REJECTED',
            )
        else:
            return Response({'error': 'mode must be auditor, billing, or rate_approver'}, status=status.HTTP_400_BAD_REQUEST)

        accepted_data = OrderListByUserIdSerializer(accepted_orders.distinct(), many=True).data
        rejected_data = OrderListByUserIdSerializer(rejected_orders.distinct(), many=True).data

        for item in accepted_data:
            item['decision_type'] = 'accepted'
        for item in rejected_data:
            item['decision_type'] = 'rejected'

        return Response(sorted(
            [*accepted_data, *rejected_data],
            key=lambda item: item.get('created_at') or '',
            reverse=True,
        ))

class DashboardKPIView(APIView):
    permission_classes = [AllowAny]

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
    permission_classes = [AllowAny]

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

        # CHART 4: Top Parties by Revenue (selected period)
        top_parties = (
            filtered_orders
            .values('card_code', 'card_name')
            .annotate(revenue=Sum('total_amount'), count=Count('id'))
            .order_by('-revenue', '-count')
        )
        top_parties_data = [
            {
                'card_code': entry['card_code'],
                'card_name': entry['card_name'],
                'count': entry['count'],
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

def extract_type_from_name(item_name):
    """Extract size/type like '1 LTR', '500 ML', '5 KG' from item name"""
    if not item_name:
        return None

    # More flexible pattern - handles various formats
    # Matches: 1 LTR, 1LTR, 1 Ltr, 1L, 500 ML, 500ML, 5 KG, 5KG, 200 GM, 200GM, etc.
    pattern = r'(\d+(?:\.\d+)?\s*(?:LTR|LITRE|LITER|L|ML|KG|KGS|GM|GMS|GRAM|G|PCS|PC|POUCH|TIN|JAR|BTL|BTL|CAN|BOTTLE|PACK|PKT|BOX)S?)\b'

    match = re.search(pattern, item_name.upper())

    if match:
        # Normalize the result (e.g., "1LTR" -> "1 LTR")
        result = match.group(1).strip()
        # Add space between number and unit if missing
        result = re.sub(r'(\d)([A-Z])', r'\1 \2', result)
        return result
    return None

class PartyProductsView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, card_code):
        normalized_card_code = (card_code or '').strip()
        active_item_codes = _active_sap_item_codes()
        assignments = PartyProductAssignment.objects.filter(
            card_code=normalized_card_code,
            is_active=True,
            item_code__in=active_item_codes,
        ).order_by('category', 'item_code')

        rows = []
        for assignment in assignments:
            product = SapProduct.objects.filter(
                active_product_q(),
                item_code=assignment.item_code,
                category=assignment.category,
            ).first()

            # Some older assignment rows can carry a valid item_code with a
            # category that no longer matches SAP metadata exactly.
            if not product:
                product = SapProduct.objects.filter(active_product_q(), item_code=assignment.item_code).first()

            if not product:
                continue

            rows.append({
                'item_code': assignment.item_code,
                'category': assignment.category,
                'basic_rate': assignment.basic_rate,
                'item_name': getattr(product, 'item_name', None),
                'sal_factor2': getattr(product, 'sal_factor2', None),
                'tax_rate': getattr(product, 'tax_rate', None),
                'sal_pack_unit': getattr(product, 'sal_pack_unit', None),
                'brand': getattr(product, 'brand', None),
                'variety': getattr(product, 'variety', None),
                'combo_scheme_id': assignment.scheme_id,
                'combo_scheme_name': assignment.scheme.scheme_name if assignment.scheme else None,
            })

        return Response(rows)

# class PartyProductsView(APIView):
#     permission_classes = [AllowAny]
#     def get(self, request, card_code):
#         from django.db import connection
#         cursor = connection.cursor()
#         cursor.execute("""
#             SELECT ppa.item_code, ppa.category, ppa.basic_rate,
#                    p.item_name, p.sal_factor2, p.tax_rate, p.sal_pack_unit,
#                    p.brand, p.variety
#             FROM party_product_assignments ppa
#             LEFT JOIN sap_products p ON ppa.item_code = p.item_code
#             WHERE ppa.card_code = %s AND ppa.is_active = true
#             ORDER BY ppa.category, p.item_name
#         """, [card_code])
    
#         columns = [col[0] for col in cursor.description]
#         rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
#         return Response(rows)
    
class PartyView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        user_id = request.user.id

        assignments = UserPartyAssignment.objects.filter(
            user_id=user_id,
            is_active=True
        ).values('card_code', 'category').distinct()

        assignment_filters = Q()
        fallback_card_codes = []
        for assignment in assignments:
            card_code = assignment.get('card_code')
            category = str(assignment.get('category') or '').strip()
            if not card_code:
                continue
            if category:
                assignment_filters |= Q(card_code=card_code, category__iexact=category)
            else:
                fallback_card_codes.append(card_code)

        parties = SapParty.objects.none()
        if assignment_filters:
            parties = parties | SapParty.objects.filter(assignment_filters)
        if fallback_card_codes:
            parties = parties | SapParty.objects.filter(card_code__in=fallback_card_codes)
        parties = parties.distinct().order_by('card_name')

        data = [
            {
                'value': p.card_code,
                'card_code': p.card_code,
                'card_name': p.card_name,
                'label': f"{p.card_name} ({p.card_code})",
                'category': p.category,
                'state': p.state,
            }
            for p in parties
        ]

        return Response(data)

class DispatchLocationListView(ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = DispatchLocationSerializer
    queryset = DispatchLocation.objects.filter(is_active=True, name__icontains='FACTORY').order_by('name')

class PartyAddressesView(APIView):

    permission_classes = [AllowAny]

    def get(self, request):
        card_code = request.query_params.get('card_code')
        category = str(request.query_params.get('category') or '').strip().upper()
        
        if not card_code:
            return Response({'error': 'card_code is required'}, status=400)

        bill_addresses = SapPartyAddress.objects.filter(
            card_code=card_code,
            address_type='B'
        )

        ship_addresses = SapPartyAddress.objects.filter(
            card_code=card_code,
            address_type='S'
        )

        if category:
            bill_addresses = bill_addresses.filter(category__iexact=category)
            ship_addresses = ship_addresses.filter(category__iexact=category)

        # if not bill_addresses.exists() and not ship_addresses.exists():
        #     try:
        #         party = SapPartyAddress.objects.filter(card_code=card_code)
        #         fallback_address = {
        #             'id': 0,
        #             # 'address_id': party.card_name if party else '',
        #             'full_address': party.address if party else '',
        #             'gst_number': None
        #         }
        #         return Response({
        #             'bill_to': [fallback_address],
        #             'ship_to': [fallback_address],
        #             'is_fallback': True
        #         })
        #     except SapPartyAddress.DoesNotExist:
        #         return Response({
        #             'bill_to': [],
        #             'ship_to': [],
        #             'is_fallback': False
        #         })

        bill_data = PartyAddressSerializer(bill_addresses, many=True).data
        ship_data = PartyAddressSerializer(ship_addresses, many=True).data

        # if not bill_data:
        #     try:
        #         party = SapPartyAddress.objects.filter(card_code=card_code).first()
        #         bill_data = [{
        #             'id': 0,
        #             # 'address_id': party.card_name,
        #             'full_address': party.address,
        #             'gst_number': None
        #         }]
        #     except Parties.DoesNotExist:
        #         pass
                
        # if not ship_data:
        #     try:
        #         party = SapPartyAddress.objects.get(card_code=card_code)
        #         ship_data = [{
        #             'id': 0,
        #             # 'address_id': party.card_name,
        #             'full_address': party.address,
        #             'gst_number': None
        #         }]
        #     except SapPartyAddress.DoesNotExist:
        #         pass

        return Response({
            'bill_to': bill_data,
            'ship_to': ship_data,
            'is_fallback': False
        })

class ProductFiltersView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        category = request.query_params.get('category')
        brand = request.query_params.get('brand')
        variety = request.query_params.get('variety')
        active_item_codes = _active_sap_item_codes()

        # Always get all categories
        categories = ProductDetails.objects.filter(
            item_code__in=active_item_codes
        ).exclude(
            category__isnull=True
        ).exclude(
            category=''
        ).values_list('category', flat=True).distinct().order_by('category')

        # Get brands - only if category is provided
        brands = []
        if category:
            brands = ProductDetails.objects.filter(
                item_code__in=active_item_codes,
                category=category
            ).exclude(
                brand__isnull=True
            ).exclude(
                brand=''
            ).values_list('brand', flat=True).distinct().order_by('brand')

        # Get varieties - only if category AND brand are provided
        varieties = []
        if category and brand:
            varieties = ProductDetails.objects.filter(
                item_code__in=active_item_codes,
                category=category,
                brand=brand
            ).exclude(
                variety__isnull=True
            ).exclude(
                variety=''
            ).values_list('variety', flat=True).distinct().order_by('variety')
    
        # Get types - ONLY if category, brand, AND variety are provided
        types = []
        if category and brand and variety:
            types_query = ProductDetails.objects.filter(
                item_code__in=active_item_codes,
                category=category,
                brand=brand,
                variety=variety
            )
        
            item_names = types_query.values_list('item_name', flat=True)
            types_set = set()
            has_others = False
        
            for name in item_names:
                item_type = extract_type_from_name(name)
                if item_type:
                    types_set.add(item_type)
                else:
                    has_others = True
        
            types = sorted(list(types_set))
        
            if has_others:
                types.append('Others')

        return Response({
            'categories': [{'label': c, 'value': c} for c in categories],
            'brands': [{'label': b, 'value': b} for b in brands],
            'varieties': [{'label': v, 'value': v} for v in varieties],
            'types': [{'label': t, 'value': t} for t in types]
        })

class ProductListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        category = request.query_params.get('category')
        brand = request.query_params.get('brand')
        variety = request.query_params.get('variety')
        item_type = request.query_params.get('type')

        products = ProductDetails.objects.filter(item_code__in=_active_sap_item_codes())

        if category:
            products = products.filter(category=category)
        if brand:
            products = products.filter(brand=brand)
        if variety:
            products = products.filter(variety=variety)
        if item_type:
            products = products.filter(item_name__icontains=item_type)

        serializer = ProductSerializer(products.order_by('item_name'), many=True)
        return Response(serializer.data)

class UpdateOrderView(APIView):
    permission_classes = [AllowAny]

    def put(self, request, order_id):
        def _to_float(value, default=0.0):
            try:
                if value in (None, ''):
                    return float(default)
                return float(value)
            except (TypeError, ValueError):
                return float(default)

        def _to_bool(value):
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            if value in (None, ""):
                return False
            return bool(value)

        order = get_object_or_404(Order, id=order_id)
        previous_status = order.status
        order_flow_type = _get_order_flow_type_for_order(order)

        serializer = CreateOrderSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        order_type = _normalize_order_type(request.data.get('order_type', order.order_type))
        employee_id = str(data.get('employee_id') or order.employee_id or '').strip()
        items = data.pop('items', [])
        order_remarks = request.data.get('remarks', data.get('remarks', ''))

        if not items:
            return Response({'error': 'At least one item is required'}, status=status.HTTP_400_BAD_REQUEST)
        if order_type == 'STAFF' and not employee_id:
            return Response({'error': 'employee_id is required for staff orders'}, status=status.HTTP_400_BAD_REQUEST)
        if order_type == 'PARTY' and not data.get('card_code'):
            return Response({'error': 'card_code is required'}, status=status.HTTP_400_BAD_REQUEST)

        # Update order header fields
        order.order_type = order_type
        order.employee_id = employee_id if order_type == 'STAFF' else data.get('employee_id', order.employee_id)
        order.card_code = data.get('card_code') or ('STAFF' if order_type == 'STAFF' else order.card_code)
        order.card_name = data.get('card_name') or (employee_id if order_type == 'STAFF' else order.card_name)
        order.bill_to_id = data.get('bill_to_id') or order.bill_to_id
        order.bill_to_address = data.get('bill_to_address', order.bill_to_address)
        order.ship_to_id = data.get('ship_to_id') or order.ship_to_id
        order.ship_to_address = data.get('ship_to_address', order.ship_to_address)
        order.dispatch_from_id = data.get('dispatch_from_id') or order.dispatch_from_id
        order.dispatch_from_name = data.get('dispatch_from_name', order.dispatch_from_name)
        order.company = data.get('company', order.company)
        order.po_number = data.get('po_number', order.po_number)
        order.is_foc = data.get('is_foc', order.is_foc)
        order.delivery_date = data.get('delivery_date') or order.delivery_date
        order.remarks = order_remarks

        # Replace items
        order.items.all().delete()

        needs_approval = False
        flagged_items = []

        for item in items:
            _create_order_item(order, item, _to_float, _to_bool)

            bp = _to_float(item.get('basic_price', 0))
            mp = _to_float(item.get('market_price', 0))
            rate_approval_reason = _get_rate_approval_reason(item, bp, mp)
            if rate_approval_reason:
                needs_approval = True
                flagged_items.append(rate_approval_reason)

        order.total_amount = sum(_to_float(item.get('total', 0)) for item in items)

        user = request.user if request.user.is_authenticated else None
        if order_type == 'STAFF':
            order.status = previous_status or get_status('Order Created')
            order.save()
            _mark_order_notifications_read(order, user)
            log_order_action(order, 'Order Created', user=user, remarks='Staff order updated')
            return Response({
                'id': order.id,
                'order_number': order.order_number,
                'total_amount': str(order.total_amount),
                'status': order.status.name if order.status else '',
                'order_type': order.order_type,
                'employee_id': order.employee_id,
                'needs_approval': False,
                'message': 'Staff order updated successfully',
            }, status=status.HTTP_200_OK)

        editor_role = getattr(getattr(user, "role", None), "name", "").lower() if user else ""
        is_billing_editor = editor_role == "billing"
        order_flow_type = _get_order_flow_type_for_order(order)

        if is_billing_editor:
            next_status = _get_next_order_flow_status(order, previous_status, 'Auditor Approval', flow_type=order_flow_type)
            flow_needs_approval = False
        else:
            next_status, flow_needs_approval = _get_initial_flow_status(
                items,
                _to_float,
                flow_type=order_flow_type,
                force_foc_flow=order.is_foc,
            )
        if next_status:
            order.status = next_status


        order.save()

        _mark_order_notifications_read(order, user)
        if next_status:
            send_order_notifications(order, next_status.name, actor=user, previous_status=previous_status)
        

        log_action = next_status.name if next_status else 'Order Created'
        log_remarks = 'Sent to auditor' if is_billing_editor else _rate_approval_remarks(flagged_items) if flow_needs_approval else ''
        if is_billing_editor:
            _close_or_create_status_log(
                order=order,
                action_status=previous_status,
                user=user,
                remarks="Accepted by billing"
            )
        log_user = _pending_log_user_for_status(next_status, user) if next_status else user
        log_order_action(order, log_action, user=log_user, remarks=log_remarks)

        return Response({
            'id': order.id,
            'order_number': order.order_number,
            'total_amount': str(order.total_amount),
            'status': order.status.name if order.status else '',
            'needs_approval': False if is_billing_editor else flow_needs_approval,
            'message': f"Order updated and sent to {next_status.name.lower()}" if next_status else 'Order updated successfully',
        }, status=status.HTTP_200_OK)

class CreateOrderView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        def _to_float(value, default=0.0):
            try:
                if value in (None, ''):
                    return float(default)
                return float(value)
            except (TypeError, ValueError):
                return float(default)

        def _to_bool(value):
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            if value in (None, ""):
                return False
            return bool(value)

        # ── Edit mode: order_id in payload means update existing order ──────
        order_id = request.data.get('order_id')
        if order_id:
            order = get_object_or_404(Order, id=int(order_id))
            previous_status = order.status
            serializer = CreateOrderSerializer(data=request.data)
            if not serializer.is_valid():
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
            data = serializer.validated_data
            order_type = _normalize_order_type(request.data.get('order_type', order.order_type))
            employee_id = str(data.get('employee_id') or order.employee_id or '').strip()
            items = data.pop('items', [])
            order_remarks = request.data.get('remarks', data.get('remarks', ''))
            if not items:
                return Response({'error': 'At least one item is required'}, status=status.HTTP_400_BAD_REQUEST)
            if order_type == 'STAFF' and not employee_id:
                return Response({'error': 'employee_id is required for staff orders'}, status=status.HTTP_400_BAD_REQUEST)
            if order_type == 'PARTY' and not data.get('card_code'):
                return Response({'error': 'card_code is required'}, status=status.HTTP_400_BAD_REQUEST)
        
            order.order_type = order_type
            order.employee_id = employee_id if order_type == 'STAFF' else data.get('employee_id', order.employee_id)
            order.card_code = data.get('card_code') or ('STAFF' if order_type == 'STAFF' else order.card_code)
            order.card_name = data.get('card_name') or (employee_id if order_type == 'STAFF' else order.card_name)
            order.bill_to_id = data.get('bill_to_id') or order.bill_to_id
            order.bill_to_address = data.get('bill_to_address', order.bill_to_address)
            order.ship_to_id = data.get('ship_to_id') or order.ship_to_id
            order.ship_to_address = data.get('ship_to_address', order.ship_to_address)
            order.dispatch_from_id = data.get('dispatch_from_id') or order.dispatch_from_id
            order.dispatch_from_name = data.get('dispatch_from_name', order.dispatch_from_name)
            order.company = data.get('company', order.company)
            order.po_number = data.get('po_number', order.po_number)
            order.is_foc = data.get('is_foc', order.is_foc)
            order.delivery_date = data.get('delivery_date') or order.delivery_date
            order.remarks = order_remarks

            order.items.all().delete()

            needs_approval = False
            flagged_items = []
            for item in items:
                _create_order_item(order, item, _to_float, _to_bool)
                bp = _to_float(item.get('basic_price', 0))
                mp = _to_float(item.get('market_price', 0))
                rate_approval_reason = _get_rate_approval_reason(item, bp, mp)
                if rate_approval_reason:
                    needs_approval = True
                    flagged_items.append(rate_approval_reason)
            assign_rate_approvers(order)

            order.total_amount = sum(_to_float(item.get('total', 0)) for item in items)
            user = request.user if request.user.is_authenticated else None
            if order_type == 'STAFF':
                order.status = previous_status or get_status('Order Created')
                order.save()
                _mark_order_notifications_read(order, user)
                log_order_action(order, 'Order Created', user=user, remarks='Staff order updated')
                return Response({
                    'id': order.id,
                    'order_number': order.order_number,
                    'total_amount': str(order.total_amount),
                    'status': order.status.name if order.status else '',
                    'order_type': order.order_type,
                    'employee_id': order.employee_id,
                    'needs_approval': False,
                    'message': 'Staff order updated successfully',
                }, status=status.HTTP_200_OK)

            editor_role = getattr(getattr(user, "role", None), "name", "").lower() if user else ""
            is_billing_editor = editor_role == "billing"
            order_flow_type = _get_order_flow_type_for_order(order)

            if is_billing_editor:
                next_status = _get_next_order_flow_status(order, previous_status, 'Auditor Approval', flow_type=order_flow_type)
                flow_needs_approval = False
            else:
                next_status, flow_needs_approval = _get_initial_flow_status(
                    items,
                    _to_float,
                    flow_type=order_flow_type,
                    force_foc_flow=order.is_foc,
                )
            if next_status:
                order.status = next_status
            order.save()

            _mark_order_notifications_read(order, user)
            if next_status:
                send_order_notifications(order, next_status.name, actor=user, previous_status=previous_status)
           

            log_action = next_status.name if next_status else 'Order Created'
            log_remarks = 'Sent to auditor' if is_billing_editor else _rate_approval_remarks(flagged_items) if flow_needs_approval else ''
            if is_billing_editor:
                _close_or_create_status_log(
                    order=order,
                    action_status=previous_status,
                    user=user,
                    remarks="Accepted by billing"
                )
            log_user = _pending_log_user_for_status(next_status, user) if next_status else user
            log_order_action(order, log_action, user=log_user, remarks=log_remarks)

            # Save every unique order as a template, but skip true duplicates.
            _save_template_if_unique(user, order)

            return Response({
                'id': order.id,
                'order_number': order.order_number,
                'total_amount': str(order.total_amount),
                'status': order.status.name if order.status else '',
                'needs_approval': False if is_billing_editor else flow_needs_approval,
                'message': f"Order updated and sent to {next_status.name.lower()}" if next_status else 'Order updated successfully',
            }, status=status.HTTP_200_OK)

        serializer = CreateOrderSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        order_type = _normalize_order_type(data.get('order_type'))
        employee_id = str(data.get('employee_id') or '').strip()
        items = data.pop('items', [])
        # Keep remarks resilient even if serializer/view code drifts across deployments.
        order_remarks = request.data.get('remarks', data.get('remarks', ''))

        if not items:
            return Response({'error': 'At least one item is required'}, status=status.HTTP_400_BAD_REQUEST)
        if order_type == 'STAFF' and not employee_id:
            return Response({'error': 'employee_id is required for staff orders'}, status=status.HTTP_400_BAD_REQUEST)
        if order_type == 'PARTY' and not data.get('card_code'):
            return Response({'error': 'card_code is required'}, status=status.HTTP_400_BAD_REQUEST)

        # Generate order number: ORD-YYYYMMDD-XXXX
        today = datetime.now().strftime('%Y%m%d')
        last_order = Order.objects.filter(
            order_number__startswith=f'ORD-{today}'
        ).order_by('-order_number').first()

        if last_order:
            last_num = int(last_order.order_number.split('-')[-1])
            new_num = last_num + 1
        else:
            new_num = 1

        order_number = f'ORD-{today}-{new_num:04d}'

        total_amount = sum(_to_float(item.get('total', 0)) for item in items)

        user = request.user if request.user.is_authenticated else None

        order = Order.objects.create(
            order_number=order_number,
            order_type=order_type,
            employee_id=employee_id,
            card_code=data.get('card_code') or ('STAFF' if order_type == 'STAFF' else ''),
            card_name=data.get('card_name') or (employee_id if order_type == 'STAFF' else ''),
            bill_to_id=data.get('bill_to_id'),
            bill_to_address=data.get('bill_to_address', ''),
            ship_to_id=data.get('ship_to_id'),
            ship_to_address=data.get('ship_to_address', ''),
            dispatch_from_id=data.get('dispatch_from_id'),
            dispatch_from_name=data.get('dispatch_from_name', ''),
            company=data.get('company', ''),
            po_number=data.get('po_number', ''),
            is_foc=data.get('is_foc', False),
            total_amount=total_amount,
            status=get_status('Order Created'),
            created_by=user,
            delivery_date=data.get('delivery_date'),
            remarks=order_remarks
            
        )

        needs_approval = False
        flagged_items = []

        for item in items:
            _create_order_item(order, item, _to_float, _to_bool)
            bp = _to_float(item.get('basic_price', 0))
            mp = _to_float(item.get('market_price', 0))
            rate_approval_reason = _get_rate_approval_reason(item, bp, mp)
            if rate_approval_reason:
                needs_approval = True
                flagged_items.append(rate_approval_reason)

        OrderRateApproval.objects.filter(order=order).delete()
        OrderItemApprovalMapping.objects.filter(order=order).delete()

        assign_rate_approvers(order)

        # Save party orders as reusable templates, but keep staff orders separate.
        if order_type != 'STAFF':
            _save_template_if_unique(user, order)

        # Log: Order created
        log_order_action(order, 'Order Created', user=user)

        if order_type == 'STAFF':
            return Response({
                'id': order.id,
                'order_number': order.order_number,
                'total_amount': str(order.total_amount),
                'status': order.status.name if order.status else '',
                'order_type': order.order_type,
                'employee_id': order.employee_id,
                'needs_approval': False,
                'flagged_items': [],
                'remarks': order.remarks or '',
                'message': 'Staff order created successfully',
            }, status=status.HTTP_201_CREATED)

        creator_role = getattr(getattr(user, "role", None), "name", "").lower() if user else ""
        is_billing_creator = creator_role == "billing"
        order_flow_type = ORDER_FLOW_TYPE_BILLING if is_billing_creator else ORDER_FLOW_TYPE_ASM

        next_status, flow_needs_approval = _get_initial_flow_status(
            items,
            _to_float,
            flow_type=order_flow_type,
            force_foc_flow=order.is_foc,
        )
        if next_status:
            order.status = next_status
            order.save()
            send_order_notifications(order, next_status.name, actor=user)

        log_user = _pending_log_user_for_status(next_status, user) if next_status else user
        log_remarks = _rate_approval_remarks(flagged_items) if flow_needs_approval else ''
        if next_status:
            log_order_action(order, next_status.name, user=log_user, remarks=log_remarks)

        return Response({
            'id': order.id,
            'order_number': order.order_number,
            'total_amount': str(order.total_amount),
            'status': order.status.name if order.status else '',
            'needs_approval': flow_needs_approval,
            'flagged_items': flagged_items if flow_needs_approval else [],
            'remarks': order.remarks or '',
            'message': f"Order sent to {next_status.name.lower()}" if next_status else 'Order created successfully',
        }, status=status.HTTP_201_CREATED)
    

class SchemeListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        from users.models import SchemeProduct
        queryset = SchemeProduct.objects.filter(is_active=True)
        state_code = (request.query_params.get('state_code') or '').strip()

        if state_code:
            state_match = State.objects.filter(
                Q(code__iexact=state_code) | Q(name__iexact=state_code),
                is_active=True,
            ).values('code', 'name').first()
            state_values = {state_code}
            if state_match:
                state_values.update(
                    value for value in (state_match.get('code'), state_match.get('name')) if value
                )
            state_filter = Q()
            for value in state_values:
                state_filter |= Q(state_code__iexact=value)
            queryset = queryset.filter(state_filter)

        schemes = queryset.order_by('scheme_name', 'scheme_id','state_code').values('scheme_id', 'scheme_name','state_code').distinct()
        return Response(list(schemes))


class OrderStatusList(APIView):
    permission_classes = [AllowAny]
    def get(self,request):
        status = OrderStatus.objects.all().values('id','name')
        return Response(list(status))

class OrderFlowConfigView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        flow_type = _normalize_order_flow_type(request.query_params.get('flow_type'))
        return Response(_order_flow_config_payload(flow_type=flow_type))

    def post(self, request):
        role_name = getattr(getattr(request.user, 'role', None), 'name', '')
        is_admin = request.user.is_staff or str(role_name).strip().lower() == 'admin'
        if not is_admin:
            return Response({'message': 'Only admin can update order flow.'}, status=status.HTTP_403_FORBIDDEN)

        flow_type = _normalize_order_flow_type(request.data.get('flow_type'))
        config = _get_order_flow_config(flow_type)
        rate_conditions = request.data.get('rate_conditions', [])
        if not isinstance(rate_conditions, list):
            return Response({'message': 'rate_conditions must be a list.'}, status=status.HTTP_400_BAD_REQUEST)

        invalid_conditions = [
            condition for condition in rate_conditions if condition not in RATE_CONDITION_CHOICES
        ]
        if invalid_conditions:
            return Response(
                {'message': 'Invalid rate condition selected.', 'invalid_conditions': invalid_conditions},
                status=status.HTTP_400_BAD_REQUEST,
            )

        config.rate_approval_enabled = bool(request.data.get('rate_approval_enabled', False))
        config.billing_enabled = bool(request.data.get('billing_enabled', False))
        config.auditor_enabled = bool(request.data.get('auditor_enabled', False))
        config.flow_type = flow_type
        config.rate_conditions = list(dict.fromkeys(rate_conditions))
        config.updated_by = request.user
        config.save()

        return Response({
            'success': True,
            'message': 'Order flow updated successfully.',
            'data': _order_flow_config_payload(config),
        })

class SchemeProductView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        queryset = SchemeProduct.objects.select_related('state').filter(is_active=True)

        product_id = request.query_params.get('product_id')
        item_code = request.query_params.get('item_code')
        scheme_id = request.query_params.get('scheme_id')
        scheme_name = request.query_params.get('scheme_name')

        if scheme_id:
            queryset = queryset.filter(scheme_id=scheme_id)
        if scheme_name:
            queryset = queryset.filter(scheme_name=scheme_name)
        if product_id:
            product = SapProduct.objects.filter(id=product_id).only('item_code').first()
            queryset = queryset.filter(item_code=product.item_code) if product else queryset.none()
        if item_code:
            queryset = queryset.filter(item_code=item_code)

        serializer = SchemeProductSerializer(queryset.order_by('scheme_name', 'scheme_id'), many=True)
        return Response({
            'success': True,
            'data': serializer.data,
            'total': len(serializer.data),
        })



class BranchView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        branches = Branches.objects.filter(bpl_name__icontains='FACTORY').order_by("bpl_name").distinct('bpl_name')
        serializer = BranchSerializer(branches, many=True)
        return Response(serializer.data)

class UpdateOrderStatusView(APIView):

    def post(self, request, order_id):
        serializer = OrderStatusUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        order = get_object_or_404(Order, id=order_id)
        previous_status = order.status
        order_flow_type = _get_order_flow_type_for_order(order)

        status_id = serializer.validated_data["status"]
        reason = serializer.validated_data.get("reason", "")
    
        # ✅ Fetch status dynamically from table
        status_obj = get_object_or_404(OrderStatus, id=status_id)

        prev_name = (previous_status.name or "").strip().lower() if previous_status else ""

        if (
            previous_status
            and previous_status.id == status_obj.id
            and _is_rejection_status(status_obj)
        ):
            return Response({
                "message": "Order already rejected",
                "order_id": order.id,
                "status": status_obj.name
            })

        # Guardrail: if client sends Auditor status again while already in Auditor stage,
        # treat it as "Auditor approved -> move to Billing Approval".
        if previous_status and previous_status.id == status_obj.id and prev_name == "auditor approval":
            billing_status = (
                OrderStatus.objects.filter(id=3).first()
                or OrderStatus.objects.filter(name__iexact="Billing Approval").first()
                or OrderStatus.objects.filter(name__iexact="Billing").first()
                or OrderStatus.objects.filter(name__icontains="billing").order_by("id").first()
            )
            if billing_status:
                status_obj = billing_status

        if previous_status and not _is_rejection_status(status_obj) and prev_name != "rate approval":
            if "billing" in prev_name or "auditor" in prev_name:
                configured_next_status = _get_next_order_flow_status(order, previous_status, flow_type=order_flow_type)
                if configured_next_status:
                    status_obj = configured_next_status

        user = request.user if request.user.is_authenticated else None

        # ✅ Update order
        order.status = status_obj

        # Only store reason when providedr
        if reason:
            order.reject_reason = reason

        _mark_order_notifications_read(order, user)


        order.save()

        new_name = (status_obj.name or "").strip().lower()
        is_auditor_to_billing = (
            prev_name == "auditor approval"
            and (status_obj.id == 3 or "billing" in new_name)
        )

        is_billing_to_auditor = (
            "billing" in prev_name
            and "auditor" in new_name
        )

        is_billing_completed = (
            "billing" in prev_name
            and (
                getattr(status_obj, "code", "") == "COMPLETED"
                or new_name == "completed"
            )
        )

        is_auditor_completed = (
            "auditor" in prev_name
            and (
                getattr(status_obj, "code", "") == "COMPLETED"
                or new_name == "completed"
            )
        )

        is_auditor_rejected = (
            "auditor" in prev_name
            and (
                getattr(status_obj, "code", "") == "REJECTED"
                or "reject" in new_name
            )
        )

        is_rate_approved = (
            prev_name == "rate approval"
            and status_obj.id == 6
        )

        is_rate_rejected = (
            prev_name == "rate approval"
            and (
                status_obj.id == APPROVER_REJECTED_ACTION_ID
                or getattr(status_obj, "code", "") == "REJECTED"
                or "reject" in new_name
            )
        )

        if is_rate_approved or is_rate_rejected:
            rate_approval = _get_order_rate_approval(order, user)
            if not rate_approval:
                order.status = previous_status
                order.save(update_fields=["status"])
                return Response(
                    {"message": "This order is not assigned to you for rate approval."},
                    status=status.HTTP_403_FORBIDDEN,
                )

            if rate_approval.status != "PENDING":
                order.status = previous_status
                order.save(update_fields=["status"])
                return Response(
                    {
                        "message": f"You have already {rate_approval.status.lower()} this order.",
                        "order_id": order.id,
                        "status": order.status.name if order.status else "",
                        "approval_status": rate_approval.status,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if is_rate_rejected:
                _mark_rate_approval_decision(order, user, "REJECTED", reason)
                _close_or_create_status_log(
                    order=order,
                    action_status=previous_status,
                    user=user,
                    remarks=reason or "Rejected by rate approver"
                )

                order.status = status_obj
                order.rejected_by = user
                order.rejected_at = timezone.now()
                if reason:
                    order.reject_reason = reason
                    order.rejection_reason = reason
                order.save()

                log_order_action(order=order, action_name=status_obj.name, user=user, remarks=reason)
                send_order_notifications(order, status_obj.name, actor=user, previous_status=previous_status)

                return Response({
                    "message": "Order rejected successfully",
                    "order_id": order.id,
                    "status": status_obj.name,
                    "approval_status": "REJECTED",
                })

            _mark_rate_approval_decision(order, user, "APPROVED", reason)
            log_order_action(order=order, action_name=status_obj.name, user=user, remarks=reason)

            if _has_pending_rate_approvals(order):
                order.status = previous_status
                order.save(update_fields=["status"])
                return Response({
                    "message": "Rate approved. Waiting for remaining approvers.",
                    "order_id": order.id,
                    "status": previous_status.name if previous_status else status_obj.name,
                    "approval_status": "APPROVED",
                    "pending_approvers": [
                        _display_user_name(approver)
                        for approver in _assigned_rate_approvers_for_order(order, status_filter="PENDING")
                    ],
                })

            _close_or_create_status_log(
                order=order,
                action_status=previous_status,
                user=user,
                remarks=reason or "Approved by all rate approvers"
            )

            next_flow_status = _get_next_order_flow_status(order, previous_status, 'Billing', flow_type=order_flow_type)
            if next_flow_status:
                order.status = next_flow_status
                order.save()
                log_order_action(
                    order=order,
                    action_name=next_flow_status.name,
                    user=_pending_log_user_for_status(next_flow_status, user),
                    remarks="",
                )
                send_order_notifications(order, next_flow_status.name, actor=user, previous_status=previous_status)

            return Response({
                "message": _order_status_message(next_flow_status, "approved") if next_flow_status else "Rate approved",
                "order_id": order.id,
                "status": next_flow_status.name if next_flow_status else status_obj.name,
                "approval_status": "APPROVED",
            })

        if is_auditor_to_billing:

            auditor_pending_log = (
                OrdersLog.objects
                .filter(order=order, action=previous_status, performed_by__isnull=True)
                .order_by("-created_at")
                .first()
            )
            if auditor_pending_log:
                auditor_pending_log.performed_by = user
                if reason:
                    auditor_pending_log.remarks = reason
                auditor_pending_log.save(update_fields=["performed_by", "remarks"])

            # Next stage entry: billing approval should be a new pending row.
            billing_pending_log = (
                OrdersLog.objects
                .filter(order=order, action=status_obj, performed_by__isnull=True)
                .order_by("-created_at")
                .first()
            )
            if not billing_pending_log:
                log_order_action(
                    order=order,
                    action_name=status_obj.name,
                    user=None,
                    remarks=""
                )

            send_order_notifications(order, status_obj.name, actor=user, previous_status=previous_status)


            return Response({
                "message": _order_status_message(status_obj, "accepted"),
                "order_id": order.id,
                "status": status_obj.name
            })

        if is_billing_to_auditor:
            _close_or_create_status_log(
                order=order,
                action_status=previous_status,
                user=user,
                remarks=reason or "Accepted by billing"
            )

            log_order_action(
                order=order,
                action_name=status_obj.name,
                user=None,
                remarks=""
            )

            send_order_notifications(order, status_obj.name, actor=user, previous_status=previous_status)


            return Response({
                "message": _order_status_message(status_obj, "accepted"),
                "order_id": order.id,
                "status": status_obj.name
            })

        if is_billing_completed:
            _close_or_create_status_log(
                order=order,
                action_status=previous_status,
                user=user,
                remarks=reason or "Accepted by billing"
            )

            log_order_action(
                order=order,
                action_name=status_obj.name,
                user=user,
                remarks=reason
            )

            send_order_notifications(order, status_obj.name, actor=user, previous_status=previous_status)


            return Response({
                "message": _order_status_message(status_obj, "accepted"),
                "order_id": order.id,
                "status": status_obj.name
            })

        if is_auditor_rejected:
            _close_or_create_status_log(
                order=order,
                action_status=previous_status,
                user=user,
                remarks=reason
            )

            log_order_action(
                order=order,
                action_name=status_obj.name,
                user=user,
                remarks=reason
            )

            send_order_notifications(order, status_obj.name, actor=user, previous_status=previous_status)


            return Response({
                "message": "Order rejected successfully",
                "order_id": order.id,
                "status": status_obj.name
            })

        if is_auditor_completed:
            auditor_completed_remarks = reason or "Sales quotation created by auditor"
            auditor_pending_log = (
                OrdersLog.objects
                .filter(order=order, action=previous_status, performed_by__isnull=True)
                .order_by("-created_at")
                .first()
            )
            if auditor_pending_log:
                auditor_pending_log.performed_by = user
                auditor_pending_log.remarks = auditor_completed_remarks
                auditor_pending_log.save(update_fields=["performed_by", "remarks"])

            log_order_action(
                order=order,
                action_name=status_obj.name,
                user=user,
                remarks=auditor_completed_remarks
            )

            send_order_notifications(order, status_obj.name, actor=user, previous_status=previous_status)


            return Response({
                "message": _order_status_message(status_obj, "accepted"),
                "order_id": order.id,
                "status": status_obj.name
            })

        # If a placeholder row exists for this status (created earlier with no performer),
        # update it instead of inserting a duplicate row.
        
        pending_log = (
            OrdersLog.objects
            .filter(order=order, action=status_obj, performed_by__isnull=True)
            .order_by("-created_at")
            .first()
        )

        if pending_log:
            pending_log.performed_by = user
            if reason:
                pending_log.remarks = reason
            pending_log.save(update_fields=["performed_by", "remarks"])
        else:
            # ✅ LOG using status name from DB
            log_order_action(
                order=order,
                action_name=status_obj.name,
                user=user,
                remarks=reason
            )

        send_order_notifications(order, status_obj.name, actor=user, previous_status=previous_status)


        return Response({
            "message": _order_status_message(status_obj),
            "order_id": order.id,
            "status": status_obj.name
        })

class OrderLogsByOrderView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, order_id):
        order = get_object_or_404(Order, id=order_id)

        logs = (
            OrdersLog.objects
            .filter(order=order)
            .exclude(action_id=1)
            .order_by('created_at', 'id')
        )

        logs = list(logs)
        current_status_name = (getattr(order.status, "name", "") or "").strip().lower()
        if current_status_name == "rate approval":
            latest_rate_log = next(
                (
                    log for log in reversed(logs)
                    if (getattr(log.action, "name", "") or "").strip().lower() == "rate approval"
                ),
                None
            )
            if latest_rate_log:
                latest_rate_log.performed_by = None
            elif order.status:
                latest_rate_log = OrdersLog.objects.create(
                    order=order,
                    action=order.status,
                    performed_by=None,
                    remarks='Rate approval pending',
                )
                logs.append(latest_rate_log)

        serializer = OrdersLogSerializer(logs, many=True)
        return Response(serializer.data)

class OrderDetailsByOrderView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, order_id):
        order = get_object_or_404(
            Order.objects.select_related("status")
            .prefetch_related("items"),
            id=order_id
        )

        serializer = OrderDetailSerializer(order)
        return Response(serializer.data)

class OrdersByUserView(APIView):
    permission_classes = [AllowAny]

    def get(self,request, user_id):
        orders = (
            Order.objects.filter(created_by=user_id)
            .select_related("status", "created_by")
            .prefetch_related("items", "items__schemes")
            .order_by("-created_at")
        )

        serializer = OrderListByUserIdSerializer(orders, many=True)

        return Response(serializer.data)
    
_status_cache = {}

def get_status(name):
    if name not in _status_cache:
        try:
            _status_cache[name] = OrderStatus.objects.get(name=name)
        except OrderStatus.DoesNotExist:
            return None
    return _status_cache[name]

def _close_or_create_status_log(order, action_status, user, remarks=''):
    if not action_status:
        return

    log = (
        OrdersLog.objects
        .filter(order=order, action=action_status, performed_by__isnull=True)
        .order_by("-created_at")
        .first()
    )

    if log:
        log.performed_by = user
        log.remarks = remarks
        log.save(update_fields=["performed_by", "remarks"])
        return

    OrdersLog.objects.create(
        order=order,
        action=action_status,
        performed_by=user,
        remarks=remarks
    )

class ApproveOrderView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, order_id):
        try:
            order = Order.objects.get(id=order_id)
        except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)
    
        if order.status.name != 'Order Created':
            return Response(
                {'error': f'Cannot approve. Current status: {order.status.name}'},
                status=status.HTTP_400_BAD_REQUEST
            )
    
        order.status = get_status('Approved')
        order.approved_by = request.user if request.user.is_authenticated else None
        order.approved_at = datetime.now()
        order.save()

        _mark_order_notifications_read(
            order,
            request.user if request.user.is_authenticated else None,
        )

        if order.status:
            send_order_notifications(
                order,
                'Approved',
                actor=request.user if request.user.is_authenticated else None,
            )

    
        return Response({
            'message': f'Order {order.order_number} approved successfully',
            'order_number': order.order_number,
            'status': order.status,
        })

class RejectOrderView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, order_id):
        try:
            order = Order.objects.get(id=order_id)
        except Order.DoesNotExist:
            return Response({'error': 'Order not found'}, status=status.HTTP_404_NOT_FOUND)
    
        if order.status.name != 'Order Created':
            return Response(
                {'error': f'Cannot reject. Current status: {order.status.name}'},
                status=status.HTTP_400_BAD_REQUEST
            )
    
        reason = request.data.get('reason', '')
    
        if not reason:
            return Response(
                {'error': 'Rejection reason is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
    
        order.status = get_status('Rejected')
        order.rejected_by = request.user if request.user.is_authenticated else None
        order.rejected_at = datetime.now()
        order.rejection_reason = reason
        order.save()

        _mark_order_notifications_read(
            order,
            request.user if request.user.is_authenticated else None,
        )
        if order.status:
            send_order_notifications(
                order,
                'Rejected',
                actor=request.user if request.user.is_authenticated else None,
            )
        
        
    
        return Response({
            'message': f'Order {order.order_number} rejected',
            'order_number': order.order_number,
            'status': order.status,
        })

class OrderListView(APIView):
    permission_classes = [AllowAny]
    
    def get(self, request):
        status_filter = request.query_params.get('status', None)
        user_id = request.query_params.get('user_id', None)
        billing_view = request.query_params.get('billing', 'false').lower() == 'true'

        if request.user.is_authenticated:
            orders = _get_base_orders(request.user)
        else:
            orders = Order.objects.all()

        if billing_view:
            # Billing view: show billing-related orders from the caller's allowed scope.
            orders = orders.filter(status_id__in=[3, 5, 6, 8])
        else:
            if status_filter:
                orders = orders.filter(status__code=status_filter)
            else:
                orders = orders.filter(sap_created=False)

        if user_id:
            orders = orders.filter(created_by=user_id)

        orders = (
            orders
            .select_related('status', 'created_by')
            .prefetch_related('items', 'rate_approvals__approver')
            .order_by('-created_at')
            .distinct()
        )

        data = []
        for order in orders:
            items_qs = order.items.all()
            items_count = items_qs.count()
            data.append({
                'id': order.id,
                'order_number': order.order_number,
                'order_type': order.order_type,
                'employee_id': order.employee_id,
                'card_code': order.card_code,
                'card_name': order.card_name,
                'total_amount': str(order.total_amount),
                'status': order.status.code,
                'status_display': order.status.name,
                'sap_doc_number': order.sap_doc_number or '',
                'items_count': items_count,
                'created_by': order.created_by.name if order.created_by else None,
                'created_at': order.created_at,
                'delivery_date': order.delivery_date,
                'po_number': order.po_number,
                'is_foc': order.is_foc,
                'bill_to_address': order.bill_to_address,
                'ship_to_address': order.ship_to_address,
                'dispatch_from_id': order.dispatch_from_id,
                'categories': list(
                    items_qs.exclude(category__isnull=True)
                    .exclude(category__exact='')
                    .values_list('category', flat=True)
                    .distinct()
                ),
                'rate_approvals': _order_rate_approval_payload(order),
                'items': OrderItemSerializer(items_qs, many=True).data
            })

        return Response(data)

class CreateSchemeView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = CreateSchemeSerializer(data=request.data)

        if serializer.is_valid():
            serializer.save()
            return Response({
                'success': True,
                'message': 'Scheme created successfully',
                'data': serializer.data
            }, status=status.HTTP_201_CREATED)

        return Response({
            'success': False,
            'message': 'Failed to create scheme',
            'errors': serializer.errors
        }, status=status.HTTP_400_BAD_REQUEST)
    
    permission_classes = [AllowAny]
class TemplatePartyListView(APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
  
        templates = Template.objects.filter(user=request.user).select_related('order')
    
        parties_dict = {}
        for t in templates:
            card_code = t.order.card_code
            if card_code not in parties_dict:
                parties_dict[card_code] = {
                    "label": f"{t.order.card_name}",
                    # "label": f"{t.order.card_name} ({card_code})",
                    "value": card_code
                }
    
        return Response(list(parties_dict.values()), status=status.HTTP_200_OK)


class TemplateOrderListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        card_code = request.query_params.get('card_code')
        if not card_code:
            return Response({"error": "card_code is required"}, status=status.HTTP_400_BAD_REQUEST)
    
        templates = Template.objects.filter(
            user=request.user,
            order__card_code=card_code
        ).select_related('order').order_by('-created_at')
    
        orders_data = []
        for t in templates:
            date_str = t.order.created_at.strftime('%d-%b-%Y') if t.order.created_at else 'Unknown Date'
            orders_data.append({
                "label": f"Order #{t.order.order_number} ({date_str}) - ₹{t.order.total_amount}",
                "value": t.order.id
            })
        
        return Response(orders_data, status=status.HTTP_200_OK)
    
def _display_user_name(user):
    if not user:
        return "Unknown"
    return getattr(user, "name", None) or getattr(user, "username", "Unknown")

def _order_rate_approval_payload(order):
    return [
        {
            'id': approval.id,
            'approver': approval.approver_id,
            'approver_name': _display_user_name(approval.approver),
            'status': approval.status,
            'remarks': approval.remarks or '',
            'approved_at': approval.approved_at,
            'created_at': approval.created_at,
        }
        for approval in order.rate_approvals.select_related('approver').all()
    ]

def _mark_order_notifications_read(order, user):
    if not order or not user:
        return
    Notification.objects.filter(order=order, user=user, is_read=False).update(is_read=True)


def _create_notification(user, order, message):
    if not user or not order or not message:
        return
    notification = Notification.objects.create(user=user, order=order, message=message)
    print(
        'Notification created:',
        {
            'notification_id': notification.id,
            'user_id': user.id,
            'order_id': order.id,
            'message': message,
        },
    )
    _send_push_notification(user, notification)


def _send_push_notification(user, notification):
    tokens = list(
        PushToken.objects.filter(user=user, is_active=True)
        .values_list('token', flat=True)
        .distinct()
    )
    if not tokens:
        print(
            'Expo push skipped: no active tokens',
            {
                'user_id': user.id,
                'notification_id': notification.id,
            },
        )
        return

    payload = [
        {
            'to': token,
            'body': notification.message,
            'sound': 'default',
            'channelId': 'default',
            'priority': 'high',
            'data': {
                'notification_id': notification.id,
                'order_id': notification.order_id,
                'screen': 'notifications',
            },
        }
        for token in tokens
        if token
    ]
    if not payload:
        return

    try:
        response = requests.post(
            'https://exp.host/--/api/v2/push/send',
            json=payload,
            headers={
                'Accept': 'application/json',
                'Accept-encoding': 'gzip, deflate',
                'Content-Type': 'application/json',
            },
            timeout=10,
        )
        response_data = {}
        try:
            response_data = response.json()
        except ValueError:
            response_data = {'raw': response.text}

        if response.status_code >= 400:
            print('Expo push notification failed:', response.status_code, response_data)
            return

        ticket_errors = [
            ticket
            for ticket in response_data.get('data', [])
            if ticket.get('status') != 'ok'
        ]
        if response_data.get('errors') or ticket_errors:
            print(
                'Expo push notification ticket errors:',
                {
                    'errors': response_data.get('errors', []),
                    'ticket_errors': ticket_errors,
                    'user_id': user.id,
                    'notification_id': notification.id,
                },
            )
            return

        print(
            'Expo push notification accepted:',
            {
                'user_id': user.id,
                'notification_id': notification.id,
                'token_count': len(tokens),
                'tickets': response_data.get('data', []),
            },
        )
    except requests.RequestException as error:
        print('Expo push notification error:', error)



def _notify_role(role_name, order, message, exclude_user=None):
    users = User.objects.filter(role__name__iexact=role_name, is_active=True)
    if exclude_user:
        users = users.exclude(id=exclude_user.id)
    for user in users:
        _create_notification(user, order, message)


def send_order_notifications(order, status_name, actor=None, previous_status=None):
    creator = order.created_by
    creator_name = _display_user_name(creator)
    actor_name = _display_user_name(actor)
    normalized_status = (status_name or "").strip().lower()
    previous_name = (getattr(previous_status, "name", "") or "").strip().lower()

    if normalized_status in {"rate approval", "need approval"}:
        assigned_approvers = _assigned_rate_approvers_for_order(
            order,
            status_filter="PENDING",
            exclude_user=actor,
        )
        if assigned_approvers:
            for approver in assigned_approvers:
                _create_notification(
                    approver,
                    order,
                    f"Order {order.order_number} from {creator_name} needs your rate approval.",
                )
        else:
            _notify_role(
                "approver",
                order,
                f"Order {order.order_number} from {creator_name} needs rate approval.",
                exclude_user=actor,
            )
        return

    if normalized_status in {"billing", "billing pending", "billing approval"}:
        for billing_user in _billing_users_for_order(order, exclude_user=actor):
            _create_notification(
                billing_user,
                order,
                f"Order {order.order_number} from {creator_name} is ready for billing.",
            )
        return

    if normalized_status == "auditor approval":
        _notify_role(
            "auditor",
            order,
            f"Order {order.order_number} from {creator_name} is ready for auditor review.",
            exclude_user=actor,
        )
        return

    if normalized_status == "billing rejected":
        _create_notification(
            creator,
            order,
            f"Order {order.order_number} was rejected by billing ({actor_name}). Please edit and resubmit.",
        )
        return

    if normalized_status == "rejected":
        source = "auditor" if "auditor" in previous_name else "approver"
        _create_notification(
            creator,
            order,
            f"Order {order.order_number} was rejected by {source} ({actor_name}).",
        )
        return

    if normalized_status == "completed":
        _create_notification(
            creator,
            order,
            f"Order {order.order_number} has been completed by auditor ({actor_name}).",
        )
        return

    if normalized_status == "approved":
        _create_notification(
            creator,
            order,
            f"Order {order.order_number} has been approved by {actor_name}.",
        )

class NotificationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        notifications = Notification.objects.filter(user=request.user).order_by('-created_at')[:50]
        serializer = NotificationSerializer(notifications, many=True)
        return Response(serializer.data)

    def post(self, request):
        # Mark all as read
        Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
        return Response({"message": "All notifications marked as read"})

    def patch(self, request, pk=None):
        # Mark single as read
        if pk:
            try:
                notification = Notification.objects.get(id=pk, user=request.user)
                notification.is_read = True
                notification.save()
                return Response({"message": "Notification marked as read"})
            except Notification.DoesNotExist:
                return Response({"error": "Notification not found"}, status=status.HTTP_404_NOT_FOUND)
        return Response({"error": "No ID provided"}, status=status.HTTP_400_BAD_REQUEST)

class PushTokenView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        token = (request.data.get('token') or request.data.get('push_token') or '').strip()
        platform = (request.data.get('platform') or '').strip()

        if not token:
            return Response({'error': 'Push token is required'}, status=status.HTTP_400_BAD_REQUEST)

        PushToken.objects.update_or_create(
            token=token,
            defaults={
                'user': request.user,
                'platform': platform,
                'is_active': True,
            },
        )
        return Response({'success': True, 'message': 'Push token registered'})


def _stock_check_number(value):
    try:
        if value in (None, ''):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _stock_check_required_qty(item):
    qty = _stock_check_number(getattr(item, 'qty', 0))
    if qty > 0:
        return qty

    boxes = _stock_check_number(getattr(item, 'boxes', 0))
    pcs = _stock_check_number(getattr(item, 'pcs', 0))
    return boxes * pcs if boxes > 0 and pcs > 0 else boxes or pcs


def _stock_check_key(item_code, category):
    return (
        str(item_code or '').strip(),
        str(category or '').strip().upper(),
    )


def _get_live_stock_by_product(item_codes_by_category):
    stock_by_product = {}

    with SAPConnection() as connection:
        for category, item_codes in item_codes_by_category.items():
            query = SAPConnection.get_live_stock_query(category, item_codes)
            if not query:
                continue

            for row in connection.execute_query(query):
                key = _stock_check_key(row.get('ItemCode'), row.get('Category') or category)
                stock_by_product[key] = _stock_check_number(row.get('OnHand'))

    return stock_by_product


class OrderStockCheckView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        if request.user.is_authenticated:
            orders = _get_base_orders(request.user)
        else:
            orders = Order.objects.all()

        orders = (
            orders
            .filter(sap_created=True)
            .select_related('status')
            .prefetch_related('items')
            .order_by('-created_at')
            .distinct()
        )

        item_codes_by_category = defaultdict(set)
        for order in orders:
            for item in order.items.all():
                item_code = str(getattr(item, 'item_code', '') or '').strip()
                category = str(getattr(item, 'category', '') or '').strip().upper()
                if item_code and category:
                    item_codes_by_category[category].add(item_code)

        try:
            stock_by_product = _get_live_stock_by_product(item_codes_by_category)
        except Exception as error:
            return Response(
                {
                    'error': 'Unable to fetch live stock from SAP.',
                    'detail': str(error),
                },
                status=status.HTTP_502_BAD_GATEWAY,
            )

        data = []
        for order in orders:
            items = []
            for item in order.items.all():
                key = _stock_check_key(item.item_code, item.category)
                items.append({
                    'item_code': item.item_code or '-',
                    'item_name': item.item_name or item.item_code or '-',
                    'category': item.category or '-',
                    'required_qty': _stock_check_required_qty(item),
                    'available_stock': stock_by_product.get(key, 0),
                })

            data.append({
                'id': order.id,
                'order_number': order.order_number,
                'date': order.created_at,
                'customer': (
                    order.employee_id or order.card_name or 'Staff'
                    if order.order_type == 'STAFF'
                    else order.card_name or order.card_code or '-'
                ),
                'order_type': 'Staff' if order.order_type == 'STAFF' else 'Party',
                'dispatch_from': order.dispatch_from_name or str(order.dispatch_from_id or '-'),
                'status': getattr(order.status, 'name', '-') or '-',
                'items': items,
            })

        return Response(data)

class StaffProductsAPIView(APIView):

    def get(self, request):
        products = SapProduct.objects.filter(active_product_q(), staff_prices__isnull=False).distinct()

        serializer = StaffProductSerializer(products, many=True)

        return Response(serializer.data)

    def post(self, request):
        products = request.data.get("products", [])
        removed_products = request.data.get("removed_products", [])
        if not isinstance(products, list):
            return Response(
                {"error": "products must be a list"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not isinstance(removed_products, list):
            return Response(
                {"error": "removed_products must be a list"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not products and not removed_products:
            return Response(
                {"error": "products or removed_products must be a non-empty list"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        saved = []
        removed = []
        errors = []

        for index, item in enumerate(removed_products):
            product_id = item.get("product_id") or item.get("id")
            item_code = item.get("item_code")
            category = str(item.get("category") or "").strip()

            if not category:
                errors.append({"index": index, "error": "category is required"})
                continue

            product = None
            if product_id:
                product = SapProduct.objects.filter(
                    id=product_id,
                    category__iexact=category,
                ).first()
            if not product and item_code:
                product = SapProduct.objects.filter(
                    item_code=item_code,
                    category__iexact=category,
                ).first()

            if not product:
                errors.append({"index": index, "error": "product not found"})
                continue

            deleted_count, _ = StaffProductPrice.objects.filter(product=product).delete()
            if deleted_count:
                removed.append({
                    "product_id": product.id,
                    "item_code": product.item_code,
                    "item_name": product.item_name,
                    "category": product.category,
                })

        for index, item in enumerate(products):
            product_id = item.get("product_id") or item.get("id")
            item_code = item.get("item_code")
            category = str(item.get("category") or "").strip()
            rate = item.get("rate")

            if not category:
                errors.append({"index": index, "error": "category is required"})
                continue

            if rate in (None, ""):
                errors.append({"index": index, "error": "rate is required"})
                continue

            try:
                rate_value = float(rate)
            except (TypeError, ValueError):
                errors.append({"index": index, "error": "rate must be a number"})
                continue

            if rate_value < 0:
                errors.append({"index": index, "error": "rate cannot be negative"})
                continue

            product = None
            if product_id:
                product = SapProduct.objects.filter(
                    active_product_q(),
                    id=product_id,
                    category__iexact=category,
                ).first()
            if not product and item_code:
                product = SapProduct.objects.filter(
                    active_product_q(),
                    item_code=item_code,
                    category__iexact=category,
                ).first()

            if not product:
                errors.append({"index": index, "error": "product not found or inactive"})
                continue

            staff_price = StaffProductPrice.objects.filter(product=product).first()
            if staff_price:
                staff_price.rate = rate_value
                staff_price.save(update_fields=["rate"])
            else:
                staff_price = StaffProductPrice.objects.create(
                    product=product,
                    rate=rate_value,
                )

            saved.append({
                "id": staff_price.id,
                "product_id": product.id,
                "item_code": product.item_code,
                "item_name": product.item_name,
                "category": product.category,
                "rate": str(staff_price.rate),
            })

        if errors:
            return Response(
                {"saved": saved, "removed": removed, "errors": errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "message": "Staff rates saved successfully",
                "saved": saved,
                "removed": removed,
            },
            status=status.HTTP_200_OK,
        )

class UserPartyView(APIView):
    
    def get(self , request):
        user = request.query_params.get("user")
        if not user:
            return Response({"error": "user parameter is required"}, status=status.HTTP_400_BAD_REQUEST)
        
        parties = (
            Order.objects.filter(created_by__id=user)
            .exclude(card_code__isnull=True)
            .exclude(card_code__exact="")
            .values("card_code", "card_name")
            .distinct()
        )
