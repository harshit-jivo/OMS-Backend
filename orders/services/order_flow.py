"""How an order moves through its configured status flow.

Lifted out of `orders/views.py` (plan item 3.2). Nothing here touches a
request, a response or a serializer: given an order, its items and a
configuration row, these decide which status comes next.

That question is asked from several places — order creation picks an INITIAL
status, `UpdateOrderStatusView` picks the NEXT one, and the FOC path has its
own ladder — which is why the rules were duplicated across a 5,900-line view
module before this. Being callable without a request is the point: the
transition rules can now be tested directly instead of through an HTTP round
trip that also exercises permissions, serialization and logging.
"""
import logging

from orders.models import (
    OrderFlowConfig,
    OrderStatus,
    PartyOrderFlowConfig,
)

logger = logging.getLogger(__name__)


DEFAULT_RATE_CONDITIONS = ['BASIC_GT_MARKET']
ORDER_FLOW_TYPE_ASM = 'ASM'
ORDER_FLOW_TYPE_BILLING = 'BILLING'
ORDER_FLOW_TYPE_CHOICES = {
    ORDER_FLOW_TYPE_ASM: 'ASM Order Flow',
    ORDER_FLOW_TYPE_BILLING: 'Billing Orders Flow',
}

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
