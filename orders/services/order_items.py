"""Building an order's lines, including the free goods they earn.

Lifted out of `orders/views.py` (plan item 3.2). This is the most
consequential rule set in the app: it decides what a customer is actually
shipped and at what price, including scheme entries from both scheme
generations and the free half of a combo pack.

It sits in front of `scheme_engine`, which computes the entries; the
code here decides which entries are ELIGIBLE (`_scheme_v2_category_allows`
gates a v2 scheme by the line's category) and turns them into OrderItem rows.

`scheme_engine` and `scheme_rules` now live beside this module rather than at
the top of the `orders` app, so every order rule sits in one package.
"""
import logging
from collections import defaultdict
from decimal import Decimal

from orders.services import scheme_engine
from orders.models import OrderItem, OrderItemScheme
from users.models import SchemeProduct

logger = logging.getLogger(__name__)



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

    # `qty` is what ships, and a SAP DocumentLine quantity is always pieces.
    # A BOX benefit therefore arrives already converted (scheme_qty), with the
    # unit it was written in kept alongside so the UI can still say "1 box".
    benefit_uom = (raw.get('benefit_uom') or '')[:10]
    benefit_qty = to_float(raw.get('benefit_qty', 0)) or scheme_qty

    return {
        'scheme': scheme_obj,
        'qty': scheme_qty,
        'scheme_v2_id': raw.get('scheme_v2_id') or raw.get('scheme_v2'),
        'benefit_id': raw.get('benefit_id') or raw.get('benefit'),
        'benefit_item_code': benefit_item_code,
        'benefit_uom': benefit_uom,
        'benefit_qty': benefit_qty,
        'computed_qty': to_float(raw.get('computed_qty', 0)),
        'is_manual_override': to_bool(raw.get('is_manual_override')),
        'scope_type': (raw.get('scope_type') or '')[:20],
        'scope_value': (raw.get('scope_value') or '')[:100],
    }


def _scheme_v2_category_allows(scheme_v2_id, line_category):
    """Whether a v2 scheme may be persisted against a line of `line_category`.

    Strict mirror of the engine's category wall (scheme_engine.resolve_schemes,
    strict_category=True) at save time, so a stale or hand-rolled client cannot
    store a scheme that the UI would never have shown. The product line category
    and the scheme's own category must both be present and identical — a blank
    anywhere is treated as a mismatch and the scheme is dropped.

    Legacy schemes (no `scheme_v2_id`) are untouched: the category rule is a v2
    concept and the old picker keeps behaving exactly as before.
    """
    if not scheme_v2_id:
        return True
    line_category = str(line_category or '').strip()
    if not line_category:
        return False
    from orders.models import Scheme
    try:
        scheme_category = (
            Scheme.objects.filter(pk=int(scheme_v2_id))
            .values_list('category', flat=True)
            .first()
        )
    except (TypeError, ValueError):
        return False
    scheme_category = str(scheme_category or '').strip()
    if not scheme_category:
        return False
    return scheme_category.casefold() == line_category.casefold()


def _extract_order_item_schemes(item, to_float, to_bool=bool):
    """Normalise the schemes on one incoming order line.

    A v2 entry qualifies on `scheme_v2_id` alone — it has no legacy
    SchemeProduct row to point at — while legacy entries still require one, so
    the old client keeps behaving exactly as before.
    """
    line_category = str(item.get('category', '') or '').strip()
    raw_schemes = item.get('schemes')
    if isinstance(raw_schemes, list):
        extracted = []
        for raw_scheme in raw_schemes:
            if not isinstance(raw_scheme, dict):
                continue
            scheme_obj = _resolve_scheme_by_id(raw_scheme.get('scheme_id') or raw_scheme.get('scheme'))
            scheme_v2_id = raw_scheme.get('scheme_v2_id') or raw_scheme.get('scheme_v2')
            scheme_qty = to_float(raw_scheme.get('scheme_qty', raw_scheme.get('qty_scheme', 0)))
            if not _scheme_v2_category_allows(scheme_v2_id, line_category):
                continue
            if (scheme_obj or scheme_v2_id) and scheme_qty > 0:
                extracted.append(_scheme_entry(raw_scheme, scheme_obj, scheme_qty, to_float, to_bool))
        return extracted

    scheme_obj = _resolve_scheme_by_id(item.get('scheme_id') or item.get('scheme'))
    scheme_v2_id = item.get('scheme_v2_id') or item.get('scheme_v2')
    scheme_qty = to_float(item.get('scheme_qty', item.get('qty_scheme', 0)))
    if not _scheme_v2_category_allows(scheme_v2_id, line_category):
        return []
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
        combo_source_code=item.get('combo_source_code') or '',
    )

    OrderItemScheme.objects.bulk_create([
        OrderItemScheme(
            order_item=order_item,
            scheme=entry['scheme'],
            qty_scheme=entry['qty'],
            scheme_v2_id=entry['scheme_v2_id'],
            benefit_id=entry['benefit_id'],
            benefit_item_code=entry['benefit_item_code'],
            benefit_uom=entry['benefit_uom'],
            benefit_qty=entry['benefit_qty'],
            computed_qty=entry['computed_qty'],
            is_manual_override=entry['is_manual_override'],
            scope_type=entry['scope_type'],
            scope_value=entry['scope_value'],
        )
        for entry in item_schemes
    ])

    return order_item


def _apply_engine_schemes(order, items, created_items):
    """Persist the giveaways the scheme engine resolves for this order.

    The client sends back the proposals it displayed, but it is not the
    authority on them. An older client, a resumed draft, or an order placed
    through a screen that never called the preview endpoint would otherwise
    save no giveaway at all — and since the SAP push builds its free lines from
    `OrderItemScheme`, the customer's free stock would silently never ship.

    Re-resolving server-side makes the engine the single source of truth for
    what is owed. Anything the client already sent for the same (line, giveaway
    item) is left alone, so a hand-typed override is never overwritten.
    """
    card_code = getattr(order, 'card_code', '') or ''
    if not card_code:
        return

    # Mirrors the preview call: one category for the order, with the engine's
    # per-line check keeping a mixed-category order honest.
    category = next(
        (str(item.get('category') or '').strip() for item in items if item.get('category')),
        '',
    )

    try:
        proposals = scheme_engine.resolve_schemes(card_code, category, items)
    except Exception:
        logger.exception(
            'Scheme engine failed for order %s; no giveaway lines added',
            getattr(order, 'id', None),
        )
        return

    # What the client already sent, so we only fill the gaps.
    already = defaultdict(set)
    for row in OrderItemScheme.objects.filter(order_item__in=[i for i in created_items if i]):
        already[row.order_item_id].add((row.benefit_item_code or '').strip().upper())

    new_rows = []
    added_qty = defaultdict(Decimal)

    for proposal in proposals:
        # No rule means the quantity is still the user's to type; proposing a
        # zero-quantity free line would ship nothing and confuse the picker.
        if proposal.qty_is_user_supplied or proposal.qty <= 0:
            continue
        if not (0 <= proposal.line_index < len(created_items)):
            continue
        order_item = created_items[proposal.line_index]
        if order_item is None:
            continue

        benefit_item_code = (proposal.benefit_item_code or '').strip()
        if not benefit_item_code:
            continue
        if benefit_item_code.upper() in already[order_item.id]:
            continue
        already[order_item.id].add(benefit_item_code.upper())

        new_rows.append(OrderItemScheme(
            order_item=order_item,
            scheme=None,
            scheme_v2_id=proposal.scheme_id,
            benefit_id=proposal.benefit_id,
            # Snapshot: the SAP push ships exactly this, so editing the scheme
            # afterwards cannot change what an approved order sends.
            benefit_item_code=benefit_item_code,
            qty_scheme=proposal.qty,
            computed_qty=proposal.qty,
            is_manual_override=False,
            scope_type=proposal.scope_type,
            scope_value=proposal.scope_value,
        ))
        added_qty[order_item.id] += proposal.qty

    if not new_rows:
        return

    OrderItemScheme.objects.bulk_create(new_rows)

    # Keep the line's own totals in step with what _create_order_item writes.
    by_id = {item.id: item for item in created_items if item}
    for order_item_id, qty in added_qty.items():
        order_item = by_id.get(order_item_id)
        if order_item is None:
            continue
        order_item.qty_scheme = (order_item.qty_scheme or Decimal('0')) + qty
        order_item.is_scheme_visible = True
        order_item.save(update_fields=['qty_scheme', 'is_scheme_visible'])

    logger.info(
        'Scheme engine added %s giveaway line(s) to order %s',
        len(new_rows), getattr(order, 'id', None),
    )
