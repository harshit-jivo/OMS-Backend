from urllib import request
from django.shortcuts import render
import re
from .serializers import SchemeProductSerializer,OrderDetailSerializer, OrderListByUserIdSerializer,OrdersLogSerializer,OrderStatusUpdateSerializer, DispatchLocationSerializer,BranchSerializer, PartyAddressSerializer,ProductSerializer,CreateOrderSerializer,OrderItemSerializer, CreateSchemeSerializer,SchemeWriteSerializer,OrderItemSchemeSerializer, NotificationSerializer,StaffProductSerializer , OrdersByItemSerializer
from .models import PartyProductAssignment,OrdersLog,Parties, Branches, DispatchLocation, UserPartyAssignment, PartyAddress,ProductDetails,Order,OrderItem,OrderStatus,log_order_action, OrderItemScheme,OrderItemScheme,Template, Notification, PushToken, WebPushSubscription, StaffProductPrice, OrderFlowConfig, PartyOrderFlowConfig, RateApproverRule,OrderRateApproval,OrderItemApprovalMapping
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from datetime import datetime
from functools import lru_cache
from rest_framework.permissions import IsAdminUser
import calendar
from django.db.models import Sum, Count, Max, F, Q, OuterRef, Subquery
from django.db.models.functions import TruncMonth
from django.utils import timezone
from collections import defaultdict
from django.shortcuts import get_object_or_404
from rest_framework import permissions
from sap_sync.models import Party as SapParty, PartyAddress as SapPartyAddress, Product as SapProduct, active_product_q, SalesQuotationLog
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
import logging
import requests
from .ai_service import get_order_summary
from .notifications import (
    NotificationEvents,
    NotificationPlan,
    NotificationTemplates,
    NotificationTypes,
    deactivate_push_token,
    deliver_notification,
    deliver_notification_to_many,
    mark_order_notifications_read,
)
from .webpush import get_vapid_public_key

logger = logging.getLogger(__name__)


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
    'BASIC_GT_MARKET': 'Price List (Basic) > Basic Price and Basic Price != 0',
    'BASIC_LT_MARKET': 'Price List (Basic) < Basic Price',
    'BASIC_EQ_MARKET': 'Price List (Basic) = Basic Price',
    'BASIC_MARKET_ZERO': 'Price List (Basic) and Basic Price = 0',
    'BASIC_ZERO_MARKET_GT_ZERO': 'Price List (Basic) = 0 and Basic Price > 0',
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

def _normalize_category(value):
    return str(value or '').strip().upper()

def _get_order_primary_category(source):
    """Return the dominant item category (upper-cased) for an order or items list."""
    if hasattr(source, 'items'):
        categories = list(source.items.values_list('category', flat=True))
    else:
        categories = [item.get('category') for item in (source or [])]
    counts = {}
    for raw in categories:
        cat = _normalize_category(raw)
        if cat:
            counts[cat] = counts.get(cat, 0) + 1
    if not counts:
        return ''
    return max(counts.items(), key=lambda kv: kv[1])[0]

def _get_party_flow_config(card_code, flow_type=ORDER_FLOW_TYPE_ASM, category=''):
    """Return the per-party, per-category, per-role flow override. Falls back to a
    category-agnostic ('') config for the party if no category-specific one exists."""
    code = str(card_code or '').strip()
    if not code:
        return None
    flow_type = _normalize_order_flow_type(flow_type)
    category = _normalize_category(category)
    if category:
        match = PartyOrderFlowConfig.objects.filter(
            card_code=code, category=category, flow_type=flow_type
        ).first()
        if match:
            return match
    return PartyOrderFlowConfig.objects.filter(card_code=code, category='', flow_type=flow_type).first()

def _party_flow_config_payload(config):
    rate_conditions = [
        condition
        for condition in (config.rate_conditions or [])
        if condition in RATE_CONDITION_CHOICES
    ]
    flow_type = _normalize_order_flow_type(config.flow_type)
    return {
        'card_code': config.card_code,
        'category': config.category or '',
        'flow_type': flow_type,
        'flow_label': ORDER_FLOW_TYPE_CHOICES.get(flow_type, flow_type),
        'rate_approval_enabled': bool(config.rate_approval_enabled),
        'billing_enabled': bool(config.billing_enabled),
        'auditor_enabled': bool(config.auditor_enabled),
        'rate_conditions': rate_conditions,
        'updated_at': config.updated_at.isoformat() if config.updated_at else None,
        'updated_by': getattr(config.updated_by, 'username', None),
    }

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

def _get_price_condition_code(price_list_basic, basic_price):
    if price_list_basic == 0 and basic_price == 0:
        return 'BASIC_MARKET_ZERO'
    if price_list_basic > basic_price:
        return 'BASIC_GT_MARKET'
    if price_list_basic < basic_price:
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
        bp = to_float(item.get('price_list_basic', 0))
        mp = to_float(item.get('basic_price', 0))
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

def _get_initial_flow_status(items, to_float, fallback_name='Billing', flow_type=ORDER_FLOW_TYPE_ASM, force_foc_flow=False, config=None):
    if force_foc_flow:
        if flow_type == ORDER_FLOW_TYPE_BILLING:
            return _get_status_by_name('Auditor Approval'), False
        return _get_status_by_name('Billing'), False

    config = config or _get_order_flow_config(flow_type)
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
        if flow_type == ORDER_FLOW_TYPE_BILLING:
            return _get_next_flow_status(current_status, fallback_name or 'Auditor Approval', flow_type=flow_type)
        return _get_next_foc_status(current_status, fallback_name or 'Billing')
    party_config = _get_party_flow_config(
        getattr(order, 'card_code', None), flow_type, _get_order_primary_category(order)
    )
    return _get_next_flow_status(current_status, fallback_name, flow_type=flow_type, config=party_config)

def _get_next_flow_status(current_status, fallback_name=None, flow_type=ORDER_FLOW_TYPE_ASM, config=None):
    if not current_status:
        return _get_status_by_name(fallback_name) if fallback_name else None

    configured_statuses = _get_configured_stage_statuses(config=config, flow_type=flow_type)
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

def _get_rate_approval_reason(item, price_list_basic, basic_price):
    if item.get('item_type') == 'SCHEME':
        return None
    
    if item.get('qty') not in (None, ''):
        qty = float(item.get('qty') or 0)
        if qty <= 0:
            return None

    item_name = item.get('item_name') or item.get('item_code') or 'Item'

    # if price_list_basic == 0:
    #     return f"{item_name}: Price List (Basic) is 0 (Basic ₹{basic_price})"

    if basic_price > 0 and basic_price < price_list_basic:
        return f"{item_name}: Basic ₹{basic_price} < Price List (Basic) ₹{price_list_basic}"

    return None

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

def _resolve_scheme_by_id(scheme_id):
    if scheme_id:
        try:
            return SchemeProduct.objects.get(scheme_id=int(scheme_id))
        except (SchemeProduct.DoesNotExist, ValueError, TypeError):
            return None
    return None

def _scheme_entry(raw, scheme_obj, scheme_qty, to_float, to_bool):
    """One granted giveaway, in the shape _create_order_item persists.

    `scheme` is the legacy SchemeProduct (kept populated for orders still placed
    through the old picker). `scheme_v2_id` / `benefit_id` / `benefit_item_code`
    come from a scheme_engine proposal the client accepted. `benefit_item_code`
    is a snapshot: the SAP push reads it rather than re-resolving the giveaway
    item, so editing a scheme cannot change what an approved order ships.
    """
    benefit_item_code = (raw.get('benefit_item_code') or '').strip() or None
    if not benefit_item_code and scheme_obj is not None:
        benefit_item_code = (getattr(scheme_obj, 'item_code', '') or '').strip() or None

    return {
        'scheme': scheme_obj,
        'qty': scheme_qty,
        'scheme_v2_id': raw.get('scheme_v2_id') or raw.get('scheme_v2'),
        'benefit_id': raw.get('benefit_id') or raw.get('benefit'),
        'benefit_item_code': benefit_item_code,
        'computed_qty': to_float(raw.get('computed_qty', 0)),
        'is_manual_override': to_bool(raw.get('is_manual_override')),
        'scope_type': (raw.get('scope_type') or '')[:20],
        'scope_value': (raw.get('scope_value') or '')[:100],
    }


def _extract_order_item_schemes(item, to_float, to_bool=bool):
    """Normalise the schemes on one incoming order line.

    A v2 entry qualifies on `scheme_v2_id` alone — it has no legacy
    SchemeProduct row to point at — while legacy entries still require one, so
    the old client keeps behaving exactly as before.
    """
    raw_schemes = item.get('schemes')
    if isinstance(raw_schemes, list):
        extracted = []
        for raw_scheme in raw_schemes:
            if not isinstance(raw_scheme, dict):
                continue
            scheme_obj = _resolve_scheme_by_id(raw_scheme.get('scheme_id') or raw_scheme.get('scheme'))
            scheme_v2_id = raw_scheme.get('scheme_v2_id') or raw_scheme.get('scheme_v2')
            scheme_qty = to_float(raw_scheme.get('scheme_qty', raw_scheme.get('qty_scheme', 0)))
            if (scheme_obj or scheme_v2_id) and scheme_qty > 0:
                extracted.append(_scheme_entry(raw_scheme, scheme_obj, scheme_qty, to_float, to_bool))
        return extracted

    scheme_obj = _resolve_scheme_by_id(item.get('scheme_id') or item.get('scheme'))
    scheme_v2_id = item.get('scheme_v2_id') or item.get('scheme_v2')
    scheme_qty = to_float(item.get('scheme_qty', item.get('qty_scheme', 0)))
    if (scheme_obj or scheme_v2_id) and scheme_qty > 0:
        return [_scheme_entry(item, scheme_obj, scheme_qty, to_float, to_bool)]
    return []

def _create_order_item(order, item, to_float, to_bool):
    item_schemes = _extract_order_item_schemes(item, to_float, to_bool)
    first_scheme = next((e['scheme'] for e in item_schemes if e['scheme']), None)
    total_scheme_qty = sum(e['qty'] for e in item_schemes)

    order_item = OrderItem.objects.create(
        order=order,
        item_code=item.get('item_code', ''),
        item_name=item.get('item_name', ''),
        category=item.get('category', ''),
        brand=item.get('brand', ''),
        sub_group=item.get('sub_group') or item.get('variety') or '',
        item_type=item.get('item_type', ''),
        qty=to_float(item.get('qty', 0)),
        pcs=to_float(item.get('pcs', 0)),
        boxes=to_float(item.get('boxes', 0)),
        ltrs=to_float(item.get('ltrs', 0)),
        price_list_basic=to_float(item.get('price_list_basic', 0)),
        basic_price=to_float(item.get('basic_price', 0)),
        total=to_float(item.get('total', 0)),
        tax_rate=to_float(item.get('tax_rate', 0)),
        scheme=first_scheme,
        qty_scheme=total_scheme_qty,
        is_scheme_visible=to_bool(item.get('is_scheme_visible')) or bool(item_schemes),
        is_auto_free=to_bool(item.get('is_auto_free')),
        combo_source_code=item.get('combo_source_code') or None,
    )

    OrderItemScheme.objects.bulk_create([
        OrderItemScheme(
            order_item=order_item,
            scheme=entry['scheme'],
            qty_scheme=entry['qty'],
            scheme_v2_id=entry['scheme_v2_id'],
            benefit_id=entry['benefit_id'],
            benefit_item_code=entry['benefit_item_code'],
            computed_qty=entry['computed_qty'],
            is_manual_override=entry['is_manual_override'],
            scope_type=entry['scope_type'],
            scope_value=entry['scope_value'],
        )
        for entry in item_schemes
    ])

    return order_item


def _normalize_order_type(value):
    order_type = str(value or 'PARTY').strip().upper()
    if order_type == 'STAFF':
        return 'STAFF'
    if order_type == 'DISTRIBUTOR':
        return 'DISTRIBUTOR'
    return 'PARTY'


# Mart / distributor flow constants — kept in one place so the queue, the create
# branch and the approve/reject endpoints never drift.
DISTRIBUTOR_COMPANY = '3'                 # every distributor order is company 3 (Mart)
DISTRIBUTOR_DISPATCH_ID = 2               # distributor orders dispatch from the factory
DISTRIBUTOR_DISPATCH_NAME = 'FACTORY'
MART_APPROVER_ROLES = {'mart_approval', 'admin'}

# OrderStatus rows already present in the DB (managed there, not via migration):
#   Mart Approval = 12 (where a submitted distributor order lands)
#   Approved      = 6  (on approve)
#   Rejected      = 7  (on reject)
MART_STATUS_PENDING_ID = 12
MART_STATUS_APPROVED_ID = 6
MART_STATUS_REJECTED_ID = 7
MART_STATUS_COMPLETED_ID = 9   # set after a successful SAP/HANA push (Phase 2)
# Tab key (from the Mart Approval queue) -> the status id it maps to.
MART_TAB_STATUS_IDS = {
    'pending': MART_STATUS_PENDING_ID,
    'approved': MART_STATUS_APPROVED_ID,
    'rejected': MART_STATUS_REJECTED_ID,
}


def _mart_status(status_id):
    """Fetch a Mart-flow status by id (rows are seeded in the DB, not migrations)."""
    return OrderStatus.objects.filter(id=status_id).first()


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
            item.sub_group or '',
            item.item_type or '',
            _normalize_template_number(item.qty),
            _normalize_template_number(item.pcs),
            _normalize_template_number(item.boxes),
            _normalize_template_number(item.ltrs),
            _normalize_template_number(item.price_list_basic),
            _normalize_template_number(item.basic_price),
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


def _get_order_template_sub_group(order):
    sub_groups = [
        str(sub_group or '').strip()
        for sub_group in order.items.values_list('sub_group', flat=True).distinct()
        if str(sub_group or '').strip()
    ]
    return ', '.join(sorted(sub_groups)) or None


def _save_template_if_unique(user, order):
    if not user:
        return

    if _has_duplicate_template(user, order):
        return

    sub_group = _get_order_template_sub_group(order)
    template, created = Template.objects.get_or_create(
        user=user,
        order=order,
        defaults={'sub_group': sub_group},
    )
    if not created and template.sub_group != sub_group:
        template.sub_group = sub_group
        template.save(update_fields=['sub_group'])


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


def _build_iexact_filter(field_name, values):
    query = Q()
    for value in values:
        query |= Q(**{f'{field_name}__iexact': value})
    return query


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
        user_categories = _get_user_category_names(user)
        if user_categories and not set(user_categories).intersection(order_categories):
            continue

        user_main_groups = _get_user_main_group_names(user)
        if user_main_groups:
            party_match = SapParty.objects.filter(
                card_code=order.card_code,
            )
            if user_categories:
                party_match = party_match.filter(category__in=user_categories)
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

def is_combo_item_name(item_name):
    """Combo packs are named "<paid part> + <free part>" in SAP."""
    return '+' in str(item_name or '')


# One free unit per combo unit: the order form counts pieces, and a combo pack
# carries one of the free product per piece. The trailing "4 PCS" / "6 PCS" in a
# combo name is the paid SKU's carton config (it equals sal_factor2), not a free
# count, so it is deliberately not parsed. Set `free_qty_per_unit` on the
# assignment for the rare pack that gives away more than one.
DEFAULT_COMBO_FREE_QTY_PER_UNIT = 1.0


def _resolve_combo_free_mapping(assignment):
    """Return (free_item_code, free_qty_per_unit) for a combo assignment.

    The mapping lives on `party_product_assignments`, so it is nominally
    per-party. A combo's free half is the same product for everyone, though, so
    a blank mapping falls back to any other party's row for the same
    item_code/category — set it once and every party picks it up.
    """
    free_item_code = (assignment.free_item_code or '').strip()
    free_qty = assignment.free_qty_per_unit

    if not free_item_code:
        donor = PartyProductAssignment.objects.filter(
            item_code=assignment.item_code,
            category=assignment.category,
            is_active=True,
        ).exclude(free_item_code__isnull=True).exclude(free_item_code='').first()
        if donor:
            free_item_code = (donor.free_item_code or '').strip()
            if free_qty is None:
                free_qty = donor.free_qty_per_unit

    if not free_item_code:
        return None, None

    qty_per_unit = float(free_qty) if free_qty is not None else DEFAULT_COMBO_FREE_QTY_PER_UNIT
    return free_item_code, (qty_per_unit if qty_per_unit > 0 else DEFAULT_COMBO_FREE_QTY_PER_UNIT)


def _serialize_free_product(free_item_code, category):
    product = (
        SapProduct.objects.filter(active_product_q(), item_code=free_item_code, category=category).first()
        or SapProduct.objects.filter(active_product_q(), item_code=free_item_code).first()
    )
    if not product:
        return None
    return {
        'item_code': product.item_code,
        'item_name': product.item_name,
        'category': product.category,
        'brand': product.brand,
        'variety': product.sub_group,
        'sub_group': product.sub_group,
        'sal_factor2': product.sal_factor2,
        'sal_pack_unit': product.sal_pack_unit,
        'tax_rate': product.tax_rate,
        # Free of cost — the auto-added order line is always zero-priced.
        'basic_rate': 0,
    }


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

            item_name = getattr(product, 'item_name', None)
            is_combo = is_combo_item_name(item_name)
            free_item_code, free_qty_per_unit = (
                _resolve_combo_free_mapping(assignment) if is_combo else (None, None)
            )
            free_product = (
                _serialize_free_product(free_item_code, assignment.category) if free_item_code else None
            )

            rows.append({
                'item_code': assignment.item_code,
                'category': assignment.category,
                'basic_rate': assignment.basic_rate,
                'item_name': item_name,
                'sal_factor2': getattr(product, 'sal_factor2', None),
                'tax_rate': getattr(product, 'tax_rate', None),
                'sal_pack_unit': getattr(product, 'sal_pack_unit', None),
                'brand': getattr(product, 'brand', None),
                # The Add Sales cascade's "Sub Group" column reads `variety`;
                # feed it the product's sub_group so the column groups by sub group.
                'variety': getattr(product, 'sub_group', None),
                'sub_group': getattr(product, 'sub_group', None),
                'combo_scheme_id': assignment.scheme_id,
                'combo_scheme_name': assignment.scheme.scheme_name if assignment.scheme else None,
                # Combo pack -> free-of-cost companion line. `free_item` is null
                # when the combo has no mapping yet, and the UI then behaves as
                # it always did.
                'is_combo': is_combo,
                'free_item_code': free_product['item_code'] if free_product else None,
                'free_qty_per_unit': free_qty_per_unit if free_product else None,
                'free_item': free_product,
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
    permission_classes = [IsAuthenticated]

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

            bp = _to_float(item.get('price_list_basic', 0))
            mp = _to_float(item.get('basic_price', 0))
            rate_approval_reason = _get_rate_approval_reason(item, bp, mp)
            if rate_approval_reason:
                needs_approval = True
                flagged_items.append(rate_approval_reason)

        order.total_amount = sum(_to_float(item.get('total', 0)) for item in items)

        user = request.user if request.user.is_authenticated else None
        if order_type == 'STAFF':
            order.status = previous_status or get_status('Order Created')
            order.save()
            mark_order_notifications_read(order, user)
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
                config=_get_party_flow_config(order.card_code, order_flow_type, _get_order_primary_category(items)),
            )
        if next_status:
            order.status = next_status


        order.save()

        mark_order_notifications_read(order, user)
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

            # Distributor edit (Mart Approval role adjusting the order): re-save the
            # lines and keep it in the Mart flow — never route through billing.
            if order_type == 'DISTRIBUTOR':
                return self._finalize_distributor_order(
                    order, items, user, _to_float, _to_bool,
                    is_edit=True, previous_status=previous_status,
                )

            order.items.all().delete()

            needs_approval = False
            flagged_items = []
            for item in items:
                _create_order_item(order, item, _to_float, _to_bool)
                bp = _to_float(item.get('price_list_basic', 0))
                mp = _to_float(item.get('basic_price', 0))
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
                mark_order_notifications_read(order, user)
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
                    config=_get_party_flow_config(order.card_code, order_flow_type, _get_order_primary_category(items)),
                )

            # If the edit sends the order back into Rate Approval, a fresh approval
            # round begins (a new pending rate-approval log is created below), so any
            # prior approver decisions must be cleared back to PENDING. Otherwise the
            # edit bypasses rate approval and existing decisions are preserved by
            # assign_rate_approvers().
            if next_status and (next_status.name or "").strip().lower() == "rate approval":
                OrderRateApproval.objects.filter(order=order).update(
                    status="PENDING",
                    approved_at=None,
                    remarks="",
                )
            if next_status:
                order.status = next_status
            order.save()

            mark_order_notifications_read(order, user)
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

        # ── Distributor (Mart / company 3) orders take their OWN path ────────
        # They never enter the billing / auditor / rate-approval flow: they are
        # simply recorded and parked at "Mart Approval" for the Mart Approval
        # role to review. This keeps the entire billing flow below untouched.
        if order_type == 'DISTRIBUTOR':
            return self._finalize_distributor_order(order, items, user, _to_float, _to_bool)

        needs_approval = False
        flagged_items = []

        for item in items:
            _create_order_item(order, item, _to_float, _to_bool)
            bp = _to_float(item.get('price_list_basic', 0))
            mp = _to_float(item.get('basic_price', 0))
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

        # A party-specific flow override (for this role's flow type + order category)
        # fully replaces the global ASM/BILLING flow.
        party_flow_config = _get_party_flow_config(
            order.card_code, order_flow_type, _get_order_primary_category(items)
        )

        next_status, flow_needs_approval = _get_initial_flow_status(
            items,
            _to_float,
            flow_type=order_flow_type,
            force_foc_flow=order.is_foc,
            config=party_flow_config,
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

    def _finalize_distributor_order(self, order, items, user, _to_float, _to_bool,
                                    is_edit=False, previous_status=None):
        """Save a distributor order's lines and park it at 'Mart Approval'.

        Deliberately isolated from the billing/auditor/rate-approval machinery:
        no rate approvers, no templates, no billing notifications. Company is
        forced to 3 (Mart). Used by both the create and edit paths.
        """
        if is_edit:
            order.items.all().delete()

        # The distributor page now builds full, billing-shaped line items
        # (brand / variety / type / pcs / boxes / ltrs / tax computed exactly
        # like Add Sales), so a distributor order stores the SAME data as a
        # normal order. Use the standard item creator for both create and edit.
        for item in items:
            _create_order_item(order, item, _to_float, _to_bool)

        order.company = DISTRIBUTOR_COMPANY
        # Distributor orders always dispatch from the factory.
        order.dispatch_from_id = DISTRIBUTOR_DISPATCH_ID
        order.dispatch_from_name = DISTRIBUTOR_DISPATCH_NAME
        order.total_amount = sum(_to_float(item.get('total', 0)) for item in items)

        # Land at the Mart Approval stage (status id 12). On a fresh submit we
        # always move it there; on an approver edit we only reset it to pending
        # if it isn't already an approved/rejected order.
        mart_status = _mart_status(MART_STATUS_PENDING_ID)
        if mart_status and (not is_edit or order.status_id == MART_STATUS_PENDING_ID):
            order.status = mart_status
        order.save()

        if user:
            mark_order_notifications_read(order, user)

        if not is_edit:
            # Two-row create log, mirroring the billing flow:
            #   • 'Order Created' (action_id 1), performed_by = the distributor.
            #   • the Mart Approval stage (action_id 12), performed_by = NULL — a
            #     "pending" marker that the order is waiting for the Mart Approval
            #     role to act. The approve/reject step then adds its own row.
            log_order_action(order, 'Order Created', user=user,
                             remarks='Distributor order submitted')
            if order.status:
                log_order_action(order, order.status.name, user=None)

        return Response({
            'id': order.id,
            'order_number': order.order_number,
            'total_amount': str(order.total_amount),
            'status': order.status.name if order.status else '',
            'order_type': order.order_type,
            'company': order.company,
            'needs_approval': False,
            'message': 'Distributor order updated successfully'
            if is_edit else 'Distributor order submitted for Mart approval',
        }, status=status.HTTP_200_OK if is_edit else status.HTTP_201_CREATED)


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

        schemes = list(
            queryset
            .order_by('scheme_name', 'scheme_id', 'state_code')
            .values('scheme_id', 'scheme_name', 'state_code', 'item_code')
        )

        # Add Sales renders the giveaway as its own line in the item list, so the
        # picker has to name the item, not just the offer. One query for the whole
        # page rather than one per scheme.
        item_codes = {s['item_code'] for s in schemes if s.get('item_code')}
        names = {}
        if item_codes:
            names = dict(
                SapProduct.objects
                .filter(active_product_q(), item_code__in=item_codes)
                .values_list('item_code', 'item_name')
            )
        for scheme in schemes:
            scheme['item_name'] = names.get(scheme.get('item_code')) or scheme.get('item_code') or ''

        return Response(schemes)


class OrderStatusList(APIView):
    permission_classes = [AllowAny]
    def get(self,request):
        status = OrderStatus.objects.all().values('id','name')
        return Response(list(status))

# Page key (see frontend GRANTABLE_ADMIN_PAGES) that unlocks Order Flow Settings.
ORDER_FLOW_PAGE_KEY = 'Order_Flow_Settings'


def _can_manage_order_flow(user):
    """Admins, or any user explicitly granted the Order Flow Settings page on
    the Permissions screen (stored in User.extra_pages)."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    role_name = getattr(getattr(user, 'role', None), 'name', '')
    if user.is_staff or str(role_name).strip().lower() == 'admin':
        return True
    extra_pages = getattr(user, 'extra_pages', None) or []
    return ORDER_FLOW_PAGE_KEY in extra_pages


class OrderFlowConfigView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        flow_type = _normalize_order_flow_type(request.query_params.get('flow_type'))
        return Response(_order_flow_config_payload(flow_type=flow_type))

    def post(self, request):
        if not _can_manage_order_flow(request.user):
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

class PartyOrderFlowConfigView(APIView):
    permission_classes = [IsAuthenticated]

    def _is_admin(self, request):
        # Admins, or users granted the Order Flow Settings page on Permissions.
        return _can_manage_order_flow(request.user)

    def _party_name_map(self, card_codes):
        """Resolve party names from the category-aware sap_parties table.

        The same card_code can belong to different parties across categories
        (e.g. CUSTA000878 is PURAN STORE under OIL and A ONE BEVERAGES under
        BEVERAGES), so we key by (card_code, category). A code-only fallback is
        kept for configs whose category is blank/unmatched.
        """
        if not card_codes:
            return {}, {}
        keyed = {}
        by_code = {}
        try:
            for row in SapParty.objects.filter(card_code__in=card_codes).values('card_code', 'card_name', 'category'):
                cat = (row.get('category') or '').strip().upper()
                keyed[(row['card_code'], cat)] = row['card_name']
                by_code.setdefault(row['card_code'], row['card_name'])
        except Exception:
            return {}, {}
        return keyed, by_code

    def get(self, request):
        configs = list(PartyOrderFlowConfig.objects.all().order_by('card_code', 'flow_type'))
        keyed_names, code_names = self._party_name_map(list({cfg.card_code for cfg in configs}))
        data = []
        for cfg in configs:
            payload = _party_flow_config_payload(cfg)
            cat = (cfg.category or '').strip().upper()
            payload['card_name'] = keyed_names.get((cfg.card_code, cat)) or code_names.get(cfg.card_code, '')
            data.append(payload)
        return Response({
            'success': True,
            'data': data,
            'flow_options': [
                {'code': code, 'label': label}
                for code, label in ORDER_FLOW_TYPE_CHOICES.items()
            ],
            'condition_options': [
                {'code': code, 'label': label}
                for code, label in RATE_CONDITION_CHOICES.items()
            ],
        })

    def _parse_parties(self, request):
        """Accept either parties=[{card_code, category}] or a plain card_codes list."""
        parties = request.data.get('parties')
        result = []
        if isinstance(parties, list) and parties:
            for entry in parties:
                if isinstance(entry, dict):
                    code = str(entry.get('card_code') or '').strip()
                    category = _normalize_category(entry.get('category'))
                else:
                    code = str(entry or '').strip()
                    category = ''
                if code:
                    result.append((code, category))
        else:
            for code in request.data.get('card_codes', []) or []:
                code = str(code).strip()
                if code:
                    result.append((code, ''))
        # de-dupe
        return list(dict.fromkeys(result))

    def post(self, request):
        if not self._is_admin(request):
            return Response({'message': 'Only admin can update order flow.'}, status=status.HTTP_403_FORBIDDEN)

        parties = self._parse_parties(request)
        if not parties:
            return Response({'message': 'Select at least one party.'}, status=status.HTTP_400_BAD_REQUEST)

        flow_type = _normalize_order_flow_type(request.data.get('flow_type'))

        rate_conditions = request.data.get('rate_conditions', [])
        if not isinstance(rate_conditions, list):
            return Response({'message': 'rate_conditions must be a list.'}, status=status.HTTP_400_BAD_REQUEST)
        invalid_conditions = [c for c in rate_conditions if c not in RATE_CONDITION_CHOICES]
        if invalid_conditions:
            return Response(
                {'message': 'Invalid rate condition selected.', 'invalid_conditions': invalid_conditions},
                status=status.HTTP_400_BAD_REQUEST,
            )

        values = {
            'rate_approval_enabled': bool(request.data.get('rate_approval_enabled', False)),
            'billing_enabled': bool(request.data.get('billing_enabled', False)),
            'auditor_enabled': bool(request.data.get('auditor_enabled', False)),
            'rate_conditions': list(dict.fromkeys(rate_conditions)),
            'updated_by': request.user,
        }

        saved = []
        for code, category in parties:
            cfg, _created = PartyOrderFlowConfig.objects.update_or_create(
                card_code=code, category=category, flow_type=flow_type, defaults=values
            )
            saved.append(_party_flow_config_payload(cfg))

        flow_label = ORDER_FLOW_TYPE_CHOICES.get(flow_type, flow_type)
        return Response({
            'success': True,
            'message': f'{flow_label} applied to {len(saved)} part{"y" if len(saved) == 1 else "ies"}.',
            'data': saved,
        })

    def delete(self, request):
        if not self._is_admin(request):
            return Response({'message': 'Only admin can update order flow.'}, status=status.HTTP_403_FORBIDDEN)

        parties = self._parse_parties(request)
        if not parties:
            return Response({'message': 'Select at least one party.'}, status=status.HTTP_400_BAD_REQUEST)

        flow_type = _normalize_order_flow_type(request.data.get('flow_type'))
        condition = Q()
        for code, category in parties:
            condition |= Q(card_code=code, category=category, flow_type=flow_type)
        PartyOrderFlowConfig.objects.filter(condition).delete()
        return Response({
            'success': True,
            'message': f'Removed custom flow for {len(parties)} part{"y" if len(parties) == 1 else "ies"}.',
            'removed': [{'card_code': code, 'category': category} for code, category in parties],
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
    permission_classes = [IsAuthenticated]

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
        actor_role = (getattr(getattr(user, "role", None), "name", "") or "").strip().lower()

        # ✅ Update order
        order.status = status_obj

        # Only store reason when providedr
        if reason:
            order.reject_reason = reason

        mark_order_notifications_read(order, user)


        order.save()

        new_name = (status_obj.name or "").strip().lower()
        is_auditor_to_billing = (
            prev_name == "auditor approval"
            and (status_obj.id == 3 or "billing" in new_name)
        )

        # A billing user forwarding an order to the auditor must leave the auditor
        # stage pending (performed_by=None). Match on the actor's role too, so this
        # holds even when the previous status name doesn't literally contain
        # "billing" (otherwise it falls through to the generic handler, which would
        # stamp the billing user as the auditor-stage performer and make the
        # Auditor Approval stage look approved).
        is_billing_to_auditor = (
            ("billing" in prev_name or actor_role == "billing")
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
    permission_classes = [IsAuthenticated]

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
    permission_classes = [IsAuthenticated]

    def get(self, request, order_id):
        order = get_object_or_404(
            Order.objects.select_related("status")
            .prefetch_related("items"),
            id=order_id
        )

        serializer = OrderDetailSerializer(order)
        return Response(serializer.data)

class OrdersByUserView(APIView):
    permission_classes = [IsAuthenticated]

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
    permission_classes = [IsAuthenticated]

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

        mark_order_notifications_read(
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
    permission_classes = [IsAuthenticated]

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

        mark_order_notifications_read(
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


# ── Mart Approval flow (distributor / company 3 orders) ──────────────────────
def _is_mart_approver(user):
    """Only the Mart Approval role (or admin) may work the Mart queue."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_staff', False):
        return True
    role_name = getattr(getattr(user, 'role', None), 'name', '')
    return str(role_name).strip().lower() in MART_APPROVER_ROLES


def _serialize_mart_order(order, *, with_items=False):
    """Compact serialization for the Mart queue list / detail."""
    data = {
        'id': order.id,
        'order_number': order.order_number,
        'order_type': order.order_type,
        'card_code': order.card_code,
        'card_name': order.card_name,
        'company': order.company,
        'total_amount': str(order.total_amount),
        'status': order.status.code if order.status else '',
        'status_id': order.status_id,
        'status_display': order.status.name if order.status else '',
        'is_pending': order.status_id == MART_STATUS_PENDING_ID,
        'bill_to_id': order.bill_to_id,
        'bill_to_address': order.bill_to_address,
        'ship_to_id': order.ship_to_id,
        'ship_to_address': order.ship_to_address,
        'po_number': order.po_number,
        'delivery_date': order.delivery_date,
        'created_by': order.created_by.name if order.created_by else None,
        'created_at': order.created_at,
        'rejection_reason': order.rejection_reason or '',
        'items_count': order.items.count(),
    }
    if with_items:
        data['items'] = [{
            'id': it.id,
            'item_code': it.item_code,
            'item_name': it.item_name,
            'category': it.category,
            'qty': str(it.qty),
            'basic_price': str(it.basic_price),
            'total': str(it.total),
        } for it in order.items.all()]
    return data


class MartOrderListView(APIView):
    """All distributor (company 3 / Mart) orders, for the Mart Approval queue.
    Optional ?status=<code> filter (e.g. MART_APPROVAL)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _is_mart_approver(request.user):
            return Response({'error': 'Not authorized for the Mart queue'},
                            status=status.HTTP_403_FORBIDDEN)

        orders = Order.objects.filter(order_type='DISTRIBUTOR')
        # Filter by the queue tab (pending/approved/rejected) -> status id.
        tab = (request.query_params.get('tab') or '').strip().lower()
        if tab in MART_TAB_STATUS_IDS:
            orders = orders.filter(status_id=MART_TAB_STATUS_IDS[tab])

        orders = (orders
                  .select_related('status', 'created_by')
                  .prefetch_related('items')
                  .order_by('-created_at')
                  .distinct())
        return Response([_serialize_mart_order(o) for o in orders])


class MartOrderDetailView(APIView):
    """One distributor order with its items, for the approver's edit screen."""
    permission_classes = [IsAuthenticated]

    def get(self, request, order_id):
        if not _is_mart_approver(request.user):
            return Response({'error': 'Not authorized'}, status=status.HTTP_403_FORBIDDEN)
        order = get_object_or_404(Order, id=order_id, order_type='DISTRIBUTOR')
        return Response(_serialize_mart_order(order, with_items=True))


class MartApproveView(APIView):
    """Approve a distributor order → 'Mart Approved'. (Phase 2 will also push the
    approved order into SAP/HANA here.)"""
    permission_classes = [IsAuthenticated]

    def post(self, request, order_id):
        if not _is_mart_approver(request.user):
            return Response({'error': 'Not authorized'}, status=status.HTTP_403_FORBIDDEN)
        order = get_object_or_404(Order, id=order_id, order_type='DISTRIBUTOR')

        approved = _mart_status(MART_STATUS_APPROVED_ID)
        if not approved:
            return Response({'error': f"Approved status (id {MART_STATUS_APPROVED_ID}) is not configured in the DB."},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        order.status = approved
        order.approved_by = request.user if request.user.is_authenticated else None
        order.approved_at = datetime.now()
        order.save()
        log_order_action(order, approved.name, user=request.user, remarks='Mart order approved')

        # TODO (Phase 2): build the SAP invoice payload and push to HANA here.
        # On a successful SAP creation, move the order to Completed (id 9):
        #   completed = _mart_status(MART_STATUS_COMPLETED_ID)
        #   order.status = completed; order.sap_created = True; order.save()
        #   log_order_action(order, completed.name, user=request.user, remarks='SAP created')

        return Response({
            'message': f'Order {order.order_number} approved',
            'order_number': order.order_number,
            'status': order.status.name,
        })


class MartRejectView(APIView):
    """Reject a distributor order with a mandatory reason → 'Mart Rejected'."""
    permission_classes = [IsAuthenticated]

    def post(self, request, order_id):
        if not _is_mart_approver(request.user):
            return Response({'error': 'Not authorized'}, status=status.HTTP_403_FORBIDDEN)
        order = get_object_or_404(Order, id=order_id, order_type='DISTRIBUTOR')

        reason = (request.data.get('reason') or '').strip()
        if not reason:
            return Response({'error': 'Rejection reason is required'},
                            status=status.HTTP_400_BAD_REQUEST)

        rejected = _mart_status(MART_STATUS_REJECTED_ID)
        if not rejected:
            return Response({'error': f"Rejected status (id {MART_STATUS_REJECTED_ID}) is not configured in the DB."},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        order.status = rejected
        order.rejected_by = request.user if request.user.is_authenticated else None
        order.rejected_at = datetime.now()
        order.rejection_reason = reason
        order.save()
        log_order_action(order, rejected.name, user=request.user, remarks=f'Rejected: {reason}')

        return Response({
            'message': f'Order {order.order_number} rejected',
            'order_number': order.order_number,
            'status': order.status.name,
        })


class OrderListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        status_filter = request.query_params.get('status', None)
        user_id = request.query_params.get('user_id', None)
        billing_view = request.query_params.get('billing', 'false').lower() == 'true'
        include_sap = request.query_params.get('include_sap', 'false').lower() == 'true'

        if request.user.is_authenticated:
            orders = _get_base_orders(request.user)
        else:
            orders = Order.objects.all()

        role = getattr(request.user, 'role', None) if request.user.is_authenticated else None
        role_name = getattr(role, 'name', '').lower() if role else ''

        if billing_view:
            # Billing view: show billing-related orders from the caller's allowed scope.
            orders = orders.filter(status_id__in=[3, 5, 6, 8])
        elif role_name == 'approver' and request.query_params.get('approval_pending', '').lower() == 'true':
            # Approver pending tab: only orders still waiting at the rate approval stage.
            orders = orders.filter(
                rate_approvals__approver=request.user,
                rate_approvals__status='PENDING',
            )
            if status_filter:
                orders = orders.filter(status__code=status_filter)
            else:
                orders = orders.filter(status__code__in=APPROVER_ACTIVE_CODES)
        elif role_name == 'approver' and status_filter:
            # Approver others: query directly to avoid double JOIN
            acted_order_ids = OrderRateApproval.objects.filter(
                approver=request.user,
                status__in=['APPROVED', 'REJECTED'],
            ).values_list('order_id', flat=True) 
            
            orders = Order.objects.filter(id__in=acted_order_ids)
        else:
            if status_filter:

                status_codes = [code for code in status_filter.split(',') if code]
                if len(status_codes) > 1:
                    orders = orders.filter(status__code__in=status_codes)
                else:
                    orders = orders.filter(status__code=status_filter)
            elif not (include_sap and role_name == 'admin'):
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

        # The SAP document number is stored on the latest successful SalesQuotationLog
        # (keyed by str(order.id)), not on Order.sap_doc_number. Build a lookup so the
        # list response can surface it for orders that were pushed to SAP.
        order_ids = [str(oid) for oid in orders.values_list('id', flat=True)]
        sap_doc_map = {}
        if order_ids:
            quotation_logs = (
                SalesQuotationLog.objects
                .filter(order_id__in=order_ids, status='SUCCESS', sap_doc_num__isnull=False)
                .order_by('order_id', '-created_at')
                .values_list('order_id', 'sap_doc_num')
            )
            for log_order_id, sap_doc_num in quotation_logs:
                if log_order_id not in sap_doc_map:
                    sap_doc_map[log_order_id] = sap_doc_num

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
                'sap_doc_number': order.sap_doc_number or sap_doc_map.get(str(order.id)) or '',
                'items_count': items_count,
                'created_by': order.created_by.name if order.created_by else None,
                'created_at': order.created_at,
                'delivery_date': order.delivery_date,
                # 'po_number': order.po_number,
                'is_foc': order.is_foc,
                # 'bill_to_address': order.bill_to_address,
                # 'ship_to_address': order.ship_to_address,
                # 'dispatch_from_id': order.dispatch_from_id,
                # 'categories': list(
                #     items_qs.exclude(category__isnull=True)
                #     .exclude(category__exact='')
                #     .values_list('category', flat=True)
                #     .distinct()
                # ),
                # 'rate_approvals': _order_rate_approval_payload(order),
                # 'items': OrderItemSerializer(items_qs, many=True).data
            })

        return Response(data)

class CreateSchemeView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = SchemeWriteSerializer(data=request.data)

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


class SchemeManageListView(APIView):
    """Full scheme rows for the Add Scheme management table.

    `SchemeListView` (/orders/schemes/) is deliberately left alone — it feeds the
    Add Sales picker and returns only scheme_id/scheme_name/state_code. Managing
    schemes needs item_code and is_active as well.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        queryset = SchemeProduct.objects.all()

        include_inactive = str(
            request.query_params.get('include_inactive') or ''
        ).strip().lower() in {'1', 'true', 'yes'}
        if not include_inactive:
            queryset = queryset.filter(is_active=True)

        state_code = (request.query_params.get('state_code') or '').strip()
        if state_code:
            queryset = queryset.filter(state_code__iexact=state_code)

        search = (request.query_params.get('search') or '').strip()
        if search:
            queryset = queryset.filter(
                Q(scheme_name__icontains=search) | Q(item_code__icontains=search)
            )

        serializer = SchemeProductSerializer(
            queryset.order_by('scheme_name', 'state_code', 'scheme_id'), many=True
        )
        return Response({
            'success': True,
            'data': serializer.data,
            'total': len(serializer.data),
        })


class SchemeDetailView(APIView):
    """Read / update / delete a single scheme."""

    permission_classes = [AllowAny]

    def _get_object(self, scheme_id):
        return SchemeProduct.objects.filter(scheme_id=scheme_id).first()

    def get(self, request, scheme_id):
        scheme = self._get_object(scheme_id)
        if not scheme:
            return Response({'success': False, 'message': 'Scheme not found'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response({'success': True, 'data': SchemeProductSerializer(scheme).data})

    def put(self, request, scheme_id):
        return self._update(request, scheme_id, partial=False)

    def patch(self, request, scheme_id):
        return self._update(request, scheme_id, partial=True)

    def _update(self, request, scheme_id, partial):
        scheme = self._get_object(scheme_id)
        if not scheme:
            return Response({'success': False, 'message': 'Scheme not found'},
                            status=status.HTTP_404_NOT_FOUND)

        serializer = SchemeWriteSerializer(scheme, data=request.data, partial=partial)
        if serializer.is_valid():
            serializer.save()
            return Response({
                'success': True,
                'message': 'Scheme updated successfully',
                'data': SchemeProductSerializer(scheme).data,
            })

        return Response({
            'success': False,
            'message': 'Failed to update scheme',
            'errors': serializer.errors,
        }, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, scheme_id):
        """Deactivate by default.

        OrderItem.scheme and OrderItemScheme.scheme are FKs with on_delete=SET_NULL,
        so a row deletion would blank the scheme on every historical order that used
        it — losing the record of what was given away. Every read path already
        filters is_active=True (the pickers, and the SAP free-line fan-out), so
        deactivating removes the scheme everywhere it matters and stays reversible.
        Pass ?hard=true to actually delete the row.
        """
        scheme = self._get_object(scheme_id)
        if not scheme:
            return Response({'success': False, 'message': 'Scheme not found'},
                            status=status.HTTP_404_NOT_FOUND)

        hard = str(request.query_params.get('hard') or '').strip().lower() in {'1', 'true', 'yes'}

        used_by_orders = OrderItemScheme.objects.filter(scheme_id=scheme_id).count()

        if hard:
            if used_by_orders:
                return Response({
                    'success': False,
                    'message': (
                        f'Cannot hard-delete: {used_by_orders} order line(s) reference '
                        'this scheme. Deactivate it instead.'
                    ),
                }, status=status.HTTP_409_CONFLICT)
            scheme.delete()
            return Response({'success': True, 'message': 'Scheme deleted', 'deactivated': False})

        scheme.is_active = False
        scheme.save(update_fields=['is_active'])
        return Response({
            'success': True,
            'message': 'Scheme deactivated',
            'deactivated': True,
            'used_by_order_lines': used_by_orders,
        })


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

def _active_users_with_role(role_name, exclude_user=None):
    """Return active users holding ``role_name`` (case-insensitive).

    Used to resolve role-scoped recipients (e.g. auditors) for the next
    workflow action. This targets a specific role -- it is never a broadcast
    to all users.
    """
    users = User.objects.filter(role__name__iexact=role_name, is_active=True)
    if exclude_user:
        users = users.exclude(id=exclude_user.id)
    return list(users)


def _resolve_notification_recipients(order, status_name, actor, previous_status):
    """Determine *who* should be notified for a status transition and *what*
    message they should receive.

    Returns a :class:`NotificationPlan` (recipients, message, title,
    event_type, notification_type). ``recipients`` is a list of ``User``
    objects -- always the exact user(s) responsible for the next action, never
    a broadcast to all users. An empty plan (no recipients) means "notify no
    one".

    Business rule (Task 3): if a required recipient is missing due to
    configuration, we log the issue and return no recipients rather than
    falling back to notifying everyone -- this avoids leaking order details to
    unrelated users.
    """
    creator = order.created_by
    creator_name = _display_user_name(creator)
    actor_name = _display_user_name(actor)
    normalized_status = (status_name or "").strip().lower()
    previous_name = (getattr(previous_status, "name", "") or "").strip().lower()

    _empty = NotificationPlan([], "", "", None, None)

    # --- Forward transitions: notify the users owning the NEXT action --------
    if normalized_status in {"rate approval", "need approval"}:
        approvers = _assigned_rate_approvers_for_order(
            order, status_filter="PENDING", exclude_user=actor
        )
        if not approvers:
            logger.warning(
                "Notification skipped: order %s reached '%s' with no assigned rate "
                "approver. Not broadcasting to all approvers.",
                order.order_number,
                status_name,
            )
            return _empty
        return NotificationPlan(
            approvers,
            NotificationTemplates.rate_approval_needed(order.order_number, creator_name),
            NotificationTemplates.TITLE_RATE_APPROVAL_REQUIRED,
            NotificationEvents.RATE_APPROVAL_REQUESTED,
            NotificationTypes.APPROVAL,
        )

    if normalized_status in {"billing", "billing pending", "billing approval"}:
        billing_users = _billing_users_for_order(order, exclude_user=actor)
        if not billing_users:
            logger.warning(
                "Notification skipped: no eligible billing user resolved for order %s.",
                order.order_number,
            )
            return _empty
        return NotificationPlan(
            billing_users,
            NotificationTemplates.billing_ready(order.order_number, creator_name),
            NotificationTemplates.TITLE_BILLING_REQUIRED,
            NotificationEvents.BILLING_REQUESTED,
            NotificationTypes.BILLING,
        )

    if normalized_status == "auditor approval":
        auditors = _active_users_with_role("auditor", exclude_user=actor)
        if not auditors:
            logger.warning(
                "Notification skipped: no active auditor configured for order %s.",
                order.order_number,
            )
            return _empty
        return NotificationPlan(
            auditors,
            NotificationTemplates.auditor_review(order.order_number, creator_name),
            NotificationTemplates.TITLE_AUDITOR_REVIEW_REQUIRED,
            NotificationEvents.AUDITOR_REVIEW_REQUESTED,
            NotificationTypes.APPROVAL,
        )

    # --- Terminal transitions: notify the order creator ----------------------
    if normalized_status in {"billing rejected", "rejected", "completed", "approved"}:
        if not creator:
            logger.warning(
                "Notification skipped: order %s has no creator to notify for status '%s'.",
                order.order_number,
                status_name,
            )
            return _empty

        if normalized_status == "billing rejected":
            return NotificationPlan(
                [creator],
                NotificationTemplates.billing_rejected(order.order_number, actor_name),
                NotificationTemplates.TITLE_ORDER_REJECTED,
                NotificationEvents.ORDER_REJECTED,
                NotificationTypes.REJECTION,
            )
        if normalized_status == "rejected":
            source = "auditor" if "auditor" in previous_name else "approver"
            return NotificationPlan(
                [creator],
                NotificationTemplates.order_rejected(order.order_number, source, actor_name),
                NotificationTemplates.TITLE_ORDER_REJECTED,
                NotificationEvents.ORDER_REJECTED,
                NotificationTypes.REJECTION,
            )
        if normalized_status == "completed":
            return NotificationPlan(
                [creator],
                NotificationTemplates.order_completed(order.order_number, actor_name),
                NotificationTemplates.TITLE_ORDER_COMPLETED,
                NotificationEvents.ORDER_COMPLETED,
                NotificationTypes.COMPLETION,
            )
        # approved
        return NotificationPlan(
            [creator],
            NotificationTemplates.order_approved(order.order_number, actor_name),
            NotificationTemplates.TITLE_ORDER_APPROVED,
            NotificationEvents.ORDER_APPROVED,
            NotificationTypes.APPROVAL,
        )

    return _empty


def send_order_notifications(order, status_name, actor=None, previous_status=None):
    """Route an order status transition to the exact user(s) responsible for
    the next action.

    Responsibilities are separated:
      * recipient + message resolution -> ``_resolve_notification_recipients``
      * persistence + Expo push        -> ``notifications.deliver_notification``

    Recipients and message text are unchanged from Phase 1; the resolver now
    additionally supplies structured push metadata (title/event_type/
    notification_type) so mobile can deep-link from the payload (Task 3).
    """
    plan = _resolve_notification_recipients(
        order, status_name, actor, previous_status
    )
    if not plan.recipients or not plan.message:
        return
    deliver_notification_to_many(
        plan.recipients,
        order,
        plan.message,
        event_type=plan.event_type,
        notification_type=plan.notification_type,
        title=plan.title,
    )

class NotificationListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        notifications = (
            Notification.objects
            .filter(user=request.user)
            .select_related('order')
            .order_by('-created_at')[:50]
        )
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

    def delete(self, request):
        """Deactivate the caller's push token (e.g. on logout).

        Optional endpoint -- older app versions that never call it are
        unaffected. Scoped to ``request.user`` so a client can only disable its
        own token, and the row is retained (is_active=False) rather than
        deleted so history/analytics stay intact.
        """
        token = (request.data.get('token') or request.data.get('push_token') or '').strip()
        if not token:
            return Response({'error': 'Push token is required'}, status=status.HTTP_400_BAD_REQUEST)

        deactivate_push_token(request.user, token)
        return Response({'success': True, 'message': 'Push token deactivated'})


class WebPushPublicKeyView(APIView):
    """Expose the VAPID public (application server) key the browser needs to
    create a Web Push subscription. The public key is not secret."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({'public_key': get_vapid_public_key()})


class WebPushSubscriptionView(APIView):
    """Register or remove a browser Web Push subscription for the caller.

    Reuses the :class:`WebPushSubscription` model. Multiple browsers/devices per
    user are supported (one row per endpoint). Never touches mobile tokens.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        subscription = request.data.get('subscription') or request.data
        endpoint = (subscription.get('endpoint') or '').strip()
        keys = subscription.get('keys') or {}
        p256dh = (keys.get('p256dh') or '').strip()
        auth = (keys.get('auth') or '').strip()

        if not endpoint or not p256dh or not auth:
            return Response(
                {'error': 'endpoint, keys.p256dh and keys.auth are required'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user_agent = (request.META.get('HTTP_USER_AGENT') or '')[:255]

        # update_or_create on the unique endpoint prevents duplicates and
        # re-homes an endpoint to the current user if it moved.
        WebPushSubscription.objects.update_or_create(
            endpoint=endpoint,
            defaults={
                'user': request.user,
                'p256dh': p256dh,
                'auth': auth,
                'user_agent': user_agent,
                'is_active': True,
            },
        )
        return Response({'success': True, 'message': 'Web push subscription saved'})

    def delete(self, request):
        subscription = request.data.get('subscription') or request.data
        endpoint = (subscription.get('endpoint') or '').strip()
        if not endpoint:
            return Response(
                {'error': 'endpoint is required'}, status=status.HTTP_400_BAD_REQUEST
            )

        WebPushSubscription.objects.filter(
            user=request.user, endpoint=endpoint
        ).update(is_active=False)
        return Response({'success': True, 'message': 'Web push subscription removed'})


class NotificationHistoryView(APIView):
    """Paginated notification history (Phase 3, Task 9).

    Unlike the legacy list endpoint (latest 50, used by mobile), this returns
    the full history with limit/offset pagination and an unread count so the
    web UI can render Unread / Read / Today / Yesterday / Older groups and
    infinite scroll. Read-state grouping by date is done client-side from
    ``created_at``.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            limit = int(request.query_params.get('limit', 20))
        except (TypeError, ValueError):
            limit = 20
        try:
            offset = int(request.query_params.get('offset', 0))
        except (TypeError, ValueError):
            offset = 0

        limit = max(1, min(limit, 100))
        offset = max(0, offset)

        status_filter = (request.query_params.get('filter') or 'all').lower()

        base = Notification.objects.filter(user=request.user).select_related('order')
        if status_filter == 'unread':
            base = base.filter(is_read=False)
        elif status_filter == 'read':
            base = base.filter(is_read=True)

        total = base.count()
        unread_count = Notification.objects.filter(
            user=request.user, is_read=False
        ).count()

        page = base.order_by('-created_at')[offset:offset + limit]
        serializer = NotificationSerializer(page, many=True)

        next_offset = offset + limit if (offset + limit) < total else None
        return Response({
            'results': serializer.data,
            'count': total,
            'unread_count': unread_count,
            'next_offset': next_offset,
        })


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


def _quotation_entries_for_orders(order_ids):
    """Map order_id -> latest successful quotation {doc_entry, doc_num}.

    The SAP Sales Quotation created when an order completes is recorded in
    SalesQuotationLog (keyed by str(order.id)). We need the DocEntry to talk to
    the SAP Service Layer and the DocNum just for display.
    """
    str_ids = [str(oid) for oid in order_ids]
    mapping = {}
    if not str_ids:
        return mapping
    logs = (
        SalesQuotationLog.objects
        .filter(order_id__in=str_ids, status='SUCCESS', sap_doc_entry__isnull=False)
        .order_by('order_id', '-created_at')
        .values_list('order_id', 'sap_doc_entry', 'sap_doc_num')
    )
    for log_order_id, doc_entry, doc_num in logs:
        # first row per order is the latest (queryset ordered by -created_at)
        if log_order_id not in mapping:
            mapping[log_order_id] = {'doc_entry': doc_entry, 'doc_num': doc_num}
    return mapping


def _is_quotation_open(row):
    """A quotation is open/cancellable when DocStatus is 'O' and not cancelled."""
    doc_status = str(row.get('DocStatus') or '').upper()
    canceled = str(row.get('CANCELED') or '').upper()
    return doc_status == 'O' and canceled != 'Y'


class QuotationStatusView(APIView):
    """Batch lookup of SAP Sales Quotation status for completed orders.

    The View Orders page calls this with the ids of completed orders so it can
    show the "Cancel Sales Quotation" button only for those whose quotation is
    still open in SAP. Degrades gracefully (empty map) if SAP is unreachable so
    the orders list still renders.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        from hana.services.services import SalesOrderService

        raw_ids = request.query_params.get('order_ids', '')
        order_ids = [oid for oid in (i.strip() for i in raw_ids.split(',')) if oid.isdigit()]
        if not order_ids:
            return Response({'success': True, 'statuses': {}})

        entry_map = _quotation_entries_for_orders(order_ids)
        doc_entries = [v['doc_entry'] for v in entry_map.values() if v['doc_entry'] is not None]

        sap_rows_by_entry = {}
        if doc_entries:
            try:
                rows = SalesOrderService().get_quotation_status(doc_entries)
                for row in rows:
                    sap_rows_by_entry[int(row['DocEntry'])] = row
            except Exception as exc:
                # SAP/HANA unreachable: return what we know but don't break the page.
                return Response(
                    {'success': False, 'statuses': {}, 'error': str(exc)},
                    status=status.HTTP_200_OK,
                )

        statuses = {}
        for order_id, info in entry_map.items():
            doc_entry = info['doc_entry']
            row = sap_rows_by_entry.get(int(doc_entry)) if doc_entry is not None else None
            statuses[order_id] = {
                'doc_entry': doc_entry,
                'doc_num': info['doc_num'],
                'doc_status': (row or {}).get('DocStatus'),
                'canceled': (row or {}).get('CANCELED'),
                'is_open': bool(row) and _is_quotation_open(row),
            }

        return Response({'success': True, 'statuses': statuses})


class CancelSalesQuotationView(APIView):
    """Cancel a completed order's SAP Sales Quotation, then mirror it in OMS.

    Only allowed for COMPLETED orders whose quotation is still open in SAP. The
    SAP cancellation is the source of truth: OMS is only updated if SAP confirms.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, order_id):
        from django.conf import settings
        from serviceLayer.service import SAPServiceLayerManager
        from hana.services.services import SalesOrderService

        try:
            order = Order.objects.select_related('status').get(pk=order_id)
        except Order.DoesNotExist:
            return Response({'success': False, 'message': 'Order not found'},
                            status=status.HTTP_404_NOT_FOUND)

        if order.status.code != 'COMPLETED':
            return Response(
                {'success': False, 'message': 'Only completed orders can have their quotation cancelled'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if order.quotation_cancelled:
            return Response(
                {'success': False, 'message': 'Quotation already cancelled'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        entry_map = _quotation_entries_for_orders([order.id])
        info = entry_map.get(str(order.id))
        doc_entry = info['doc_entry'] if info else None
        doc_num = info['doc_num'] if info else None
        if doc_entry is None:
            return Response(
                {'success': False, 'message': 'No SAP sales quotation found for this order'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Confirm the quotation is still open before the destructive call.
        try:
            rows = SalesOrderService().get_quotation_status([doc_entry])
            current = next((r for r in rows if int(r['DocEntry']) == int(doc_entry)), None)
        except Exception as exc:
            return Response(
                {'success': False, 'message': f'Could not verify quotation status in SAP: {exc}'},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        if not current or not _is_quotation_open(current):
            return Response(
                {'success': False, 'message': 'Quotation is not open in SAP and cannot be cancelled'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cancel_url = f"{settings.HANA_SERVICE_LAYER_URL}/Quotations({int(doc_entry)})/Cancel"
        try:
            session = SAPServiceLayerManager.get_session()
            sap_response = session.post(cancel_url, timeout=20)
            if sap_response.status_code == 401:
                SAPServiceLayerManager.clear_session()
                session = SAPServiceLayerManager.get_session()
                sap_response = session.post(cancel_url, timeout=20)
        except Exception as exc:
            return Response(
                {'success': False, 'message': f'SAP request failed: {exc}'},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if sap_response.status_code not in (200, 201, 204):
            try:
                details = sap_response.json()
            except Exception:
                details = sap_response.text
            return Response(
                {'success': False, 'message': 'SAP rejected the cancellation', 'details': details},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # SAP confirmed — mirror in OMS.
        actor = request.user if getattr(request.user, 'is_authenticated', False) else None
        order.quotation_cancelled = True
        order.quotation_cancelled_at = timezone.now()
        order.quotation_cancelled_by = actor
        order.save(update_fields=['quotation_cancelled', 'quotation_cancelled_at', 'quotation_cancelled_by'])

        try:
            from audit.models import AuditLog
            AuditLog.objects.create(
                user=actor,
                username=getattr(actor, 'username', '') or '',
                page='View Orders',
                action='Cancelled',
                record=f'Order {order.order_number} (Quotation {doc_num})',
                field='sales_quotation',
                old_value='Open',
                new_value='Cancelled',
            )
        except Exception:
            pass  # auditing must never block the operation

        return Response({
            'success': True,
            'message': 'Sales quotation cancelled',
            'order_id': order.id,
            'doc_num': doc_num,
        })


class QuotationOverviewView(APIView):
    """Admin overview of every completed order and its SAP sales-quotation
    status (CANCELLED / OPEN / CLOSED / UNKNOWN). Powers the admin
    "Sales Quotation" screen on web and mobile.

    Degrades gracefully if SAP is unreachable: cancelled orders are still
    reported from OMS, and the rest show UNKNOWN with a sap_error note.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from hana.services.services import SalesOrderService

        role_name = getattr(getattr(request.user, 'role', None), 'name', '')
        is_admin = request.user.is_staff or str(role_name).strip().lower() == 'admin'
        if not is_admin:
            return Response(
                {'success': False, 'message': 'Only admin can view quotation status'},
                status=status.HTTP_403_FORBIDDEN,
            )

        completed = (
            Order.objects
            .filter(status__code='COMPLETED')
            .select_related('quotation_cancelled_by')
            .order_by('-created_at')
        )

        order_ids = [str(order.id) for order in completed]
        entry_map = _quotation_entries_for_orders(order_ids)
        doc_entries = [v['doc_entry'] for v in entry_map.values() if v['doc_entry'] is not None]

        sap_rows_by_entry = {}
        sap_error = None
        if doc_entries:
            try:
                rows = SalesOrderService().get_quotation_status(doc_entries)
                for row in rows:
                    sap_rows_by_entry[int(row['DocEntry'])] = row
            except Exception as exc:
                sap_error = str(exc)

        data = []
        for order in completed:
            info = entry_map.get(str(order.id)) or {}
            doc_entry = info.get('doc_entry')
            doc_num = info.get('doc_num')
            row = sap_rows_by_entry.get(int(doc_entry)) if doc_entry is not None else None

            if order.quotation_cancelled:
                quotation_status = 'CANCELLED'
            elif row is not None:
                quotation_status = 'OPEN' if _is_quotation_open(row) else 'CLOSED'
            else:
                quotation_status = 'UNKNOWN'

            data.append({
                'id': order.id,
                'order_number': order.order_number,
                'card_code': order.card_code,
                'card_name': order.card_name,
                'created_at': order.created_at,
                'doc_num': doc_num,
                'doc_entry': doc_entry,
                'quotation_cancelled': order.quotation_cancelled,
                'quotation_cancelled_at': order.quotation_cancelled_at,
                'quotation_cancelled_by': getattr(order.quotation_cancelled_by, 'username', None),
                'quotation_status': quotation_status,
            })

        return Response({'success': True, 'data': data, 'sap_error': sap_error})



class GetOrdersByItemView(APIView):
    # permission_classes = [IsAuthenticate]

    def get(self, request):
        item_code = request.query_params.get('item_code')
        if not item_code:
            return Response({"error": "item_code is required"}, status=status.HTTP_400_BAD_REQUEST)

        orders = OrderItem.objects.filter(item_code=item_code).select_related('order').order_by('-order__created_at')

        serializer = OrdersByItemSerializer(orders, many=True)
        return Response(serializer.data)

# ---------------------------------------------------------------------------
# Scheme engine v2 (see docs/scheme-architecture.md)
#
# Permission classes match the existing scheme views (AllowAny) so this lands
# behind the same gate as SchemeManageListView / SchemeDetailView rather than
# introducing a second, inconsistent auth story mid-migration.
# ---------------------------------------------------------------------------

from django.db import transaction as _db_transaction

from .models import Scheme, SchemeAssignment
from .serializers import SchemeV2Serializer, SchemeAssignmentSerializer
from . import scheme_engine


class SchemeV2ListCreateView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        queryset = Scheme.objects.prefetch_related('benefits', 'triggers', 'assignments')

        include_inactive = str(
            request.query_params.get('include_inactive') or ''
        ).strip().lower() in {'1', 'true', 'yes'}
        if not include_inactive:
            queryset = queryset.filter(is_active=True)

        search = (request.query_params.get('search') or '').strip()
        if search:
            queryset = queryset.filter(Q(code__icontains=search) | Q(name__icontains=search))

        # ?category=OIL keeps the uncategorised ("every category") schemes too —
        # they apply to OIL as much as to anything else.
        category = (request.query_params.get('category') or '').strip()
        if category:
            queryset = queryset.filter(Q(category='') | Q(category__iexact=category))

        # Filter by who a scheme reaches, e.g. ?scope_type=STATE&scope_value=PB
        scope_type = (request.query_params.get('scope_type') or '').strip()
        scope_value = (request.query_params.get('scope_value') or '').strip()
        if scope_type:
            scope_q = Q(assignments__scope_type=scope_type, assignments__is_active=True)
            if scope_value:
                scope_q &= Q(assignments__scope_value__iexact=scope_value)
            queryset = queryset.filter(scope_q).distinct()

        serializer = SchemeV2Serializer(queryset, many=True)
        return Response({'success': True, 'data': serializer.data, 'total': len(serializer.data)})

    def post(self, request):
        serializer = SchemeV2Serializer(data=request.data, context={'request': request})
        if not serializer.is_valid():
            return Response({'success': False, 'message': 'Failed to create scheme',
                             'errors': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
        with _db_transaction.atomic():
            serializer.save()
        return Response({'success': True, 'message': 'Scheme created', 'data': serializer.data},
                        status=status.HTTP_201_CREATED)


class SchemeV2DetailView(APIView):
    permission_classes = [AllowAny]

    def _get_object(self, scheme_id):
        return (
            Scheme.objects
            .prefetch_related('benefits', 'triggers', 'assignments')
            .filter(pk=scheme_id)
            .first()
        )

    def get(self, request, scheme_id):
        scheme = self._get_object(scheme_id)
        if not scheme:
            return Response({'success': False, 'message': 'Scheme not found'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response({'success': True, 'data': SchemeV2Serializer(scheme).data})

    def patch(self, request, scheme_id):
        scheme = self._get_object(scheme_id)
        if not scheme:
            return Response({'success': False, 'message': 'Scheme not found'},
                            status=status.HTTP_404_NOT_FOUND)
        serializer = SchemeV2Serializer(scheme, data=request.data, partial=True,
                                        context={'request': request})
        if not serializer.is_valid():
            return Response({'success': False, 'message': 'Failed to update scheme',
                             'errors': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)
        with _db_transaction.atomic():
            serializer.save()
        return Response({'success': True, 'message': 'Scheme updated', 'data': serializer.data})

    def delete(self, request, scheme_id):
        """Deactivate by default.

        OrderItemScheme.scheme_v2 is PROTECT, so a scheme referenced by any order
        line cannot be deleted at all -- the giveaway record has to survive.
        Deactivating removes it from every read path and stays reversible.
        """
        scheme = self._get_object(scheme_id)
        if not scheme:
            return Response({'success': False, 'message': 'Scheme not found'},
                            status=status.HTTP_404_NOT_FOUND)

        hard = str(request.query_params.get('hard') or '').strip().lower() in {'1', 'true', 'yes'}
        used_by_orders = OrderItemScheme.objects.filter(scheme_v2_id=scheme_id).count()

        if hard:
            if used_by_orders:
                return Response({
                    'success': False,
                    'message': (f'Cannot hard-delete: {used_by_orders} order line(s) reference '
                                'this scheme. Deactivate it instead.'),
                }, status=status.HTTP_409_CONFLICT)
            scheme.delete()
            return Response({'success': True, 'message': 'Scheme deleted', 'deactivated': False})

        scheme.is_active = False
        scheme.save(update_fields=['is_active', 'updated_at'])
        return Response({'success': True, 'message': 'Scheme deactivated', 'deactivated': True,
                         'used_by_order_lines': used_by_orders})


class SchemeAssignmentView(APIView):
    """Target a scheme at a party, a state, a main group, a category, or everyone.

    One STATE row reaches every vendor in that state -- including ones onboarded
    later -- which is the whole point of separating assignment from the offer.
    """

    permission_classes = [AllowAny]

    def get(self, request, scheme_id):
        if not Scheme.objects.filter(pk=scheme_id).exists():
            return Response({'success': False, 'message': 'Scheme not found'},
                            status=status.HTTP_404_NOT_FOUND)
        rows = SchemeAssignment.objects.filter(scheme_id=scheme_id).select_related('scheme')
        return Response({'success': True, 'data': SchemeAssignmentSerializer(rows, many=True).data})

    def post(self, request, scheme_id):
        scheme = Scheme.objects.filter(pk=scheme_id).first()
        if not scheme:
            return Response({'success': False, 'message': 'Scheme not found'},
                            status=status.HTTP_404_NOT_FOUND)

        # Accept a single object or a list, so "assign to these 40 parties" is one call.
        payload = request.data if isinstance(request.data, list) else [request.data]
        serializer = SchemeAssignmentSerializer(data=payload, many=True)
        if not serializer.is_valid():
            return Response({'success': False, 'message': 'Failed to assign scheme',
                             'errors': serializer.errors}, status=status.HTTP_400_BAD_REQUEST)

        user = request.user if getattr(request.user, 'is_authenticated', False) else None
        saved = []
        with _db_transaction.atomic():
            for row in serializer.validated_data:
                row.pop('scheme', None)
                obj, _created = SchemeAssignment.objects.update_or_create(
                    scheme=scheme,
                    scope_type=row['scope_type'],
                    scope_value=row.get('scope_value', ''),
                    category=row.get('category', ''),
                    defaults={
                        'is_exclusion': row.get('is_exclusion', False),
                        'valid_from': row.get('valid_from'),
                        'valid_to': row.get('valid_to'),
                        'is_active': row.get('is_active', True),
                        'created_by': user,
                    },
                )
                saved.append(obj)

        return Response({'success': True, 'message': f'{len(saved)} assignment(s) saved',
                         'data': SchemeAssignmentSerializer(saved, many=True).data},
                        status=status.HTTP_201_CREATED)

    def delete(self, request, scheme_id):
        assignment_id = request.query_params.get('assignment_id')
        if not assignment_id:
            return Response({'success': False, 'message': 'assignment_id is required'},
                            status=status.HTTP_400_BAD_REQUEST)
        deleted, _ = SchemeAssignment.objects.filter(scheme_id=scheme_id, pk=assignment_id).delete()
        if not deleted:
            return Response({'success': False, 'message': 'Assignment not found'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response({'success': True, 'message': 'Assignment removed'})


class SchemePreviewView(APIView):
    """Dry-run the engine over a draft order.

    Body: {card_code, category, lines: [{item_code, category, sub_group, brand,
    qty, pcs, boxes, ltrs, is_auto_free, combo_source_code, item_type}, ...]}

    This is what makes state-wide targeting usable -- the salesperson never picks
    a scheme from a dropdown, the engine proposes and they confirm.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        card_code = (request.data.get('card_code') or '').strip()
        if not card_code:
            return Response({'success': False, 'message': 'card_code is required'},
                            status=status.HTTP_400_BAD_REQUEST)

        lines = request.data.get('lines') or []
        if not isinstance(lines, list):
            return Response({'success': False, 'message': 'lines must be a list'},
                            status=status.HTTP_400_BAD_REQUEST)

        category = (request.data.get('category') or '').strip()
        ctx = scheme_engine.build_party_context(card_code, category)
        proposals = scheme_engine.resolve_schemes(card_code, category, lines, ctx=ctx)

        return Response({
            'success': True,
            'context': {
                'card_code': ctx.card_code,
                'category': ctx.category,
                'state_code': ctx.state_code,
                'main_group': ctx.main_group,
            },
            'proposals': [p.as_dict() for p in proposals],
        })


class SchemeApplicableView(APIView):
    """Everything reaching a vendor, with the scope that let each scheme in --
    the first question anyone asks about an unexpected giveaway."""

    permission_classes = [AllowAny]

    def get(self, request):
        card_code = (request.query_params.get('card_code') or '').strip()
        if not card_code:
            return Response({'success': False, 'message': 'card_code is required'},
                            status=status.HTTP_400_BAD_REQUEST)
        category = (request.query_params.get('category') or '').strip()
        rows = scheme_engine.applicable_schemes(card_code, category)
        return Response({'success': True, 'data': rows, 'total': len(rows)})
