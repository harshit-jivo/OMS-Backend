"""On-hand stock of an invoice log's FG items, in that log's own warehouse.

The stock lives in HANA (OITW), not in OMS, so a naive per-row lookup would mean
one HANA round trip per invoice line on a list endpoint. Instead the view builds
one map for every log it is about to serialize — a single query per
branch/warehouse pair — and hands it to the serializer through the context.
"""

import logging

from hana.services.services import SalesOrderService

logger = logging.getLogger(__name__)

# Logs written before `branch` was populated carry NULL; every one of them is
# from the oil company DB.
DEFAULT_BRANCH = 'OIL'

CONTEXT_KEY = 'fg_stock'


def _branch_of(invoice_log):
    return (getattr(invoice_log, 'branch', None) or DEFAULT_BRANCH).upper()


def _key_of(invoice_log):
    """(branch, warehouse) this log's stock should be read from, or None."""
    warehouse = getattr(invoice_log, 'warehouse', None)
    if not warehouse:
        return None
    return (_branch_of(invoice_log), warehouse)


def extract_fg_item_codes(payload):
    """FG item codes on an invoice payload, in the order the lines appear."""
    lines = (payload or {}).get('DocumentLines') or []
    codes = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        code = line.get('ItemCode')
        if not code:
            continue
        code = str(code)
        if code.upper().startswith('FG') and code not in codes:
            codes.append(code)
    return codes


def _to_number(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_fg_stock_map(invoice_logs):
    """{(branch, warehouse): {item_code: {'item_name', 'warehouse_stock'}}}

    A HANA failure is not allowed to take the invoice list down with it: the
    pair is simply left out of the map and the stock field serializes as null.
    """
    wanted = {}
    for log in invoice_logs:
        key = _key_of(log)
        codes = extract_fg_item_codes(getattr(log, 'invoice_payload', None))
        if key and codes:
            wanted.setdefault(key, set()).update(codes)

    service = SalesOrderService()
    stock_map = {}
    for (branch, warehouse), codes in wanted.items():
        try:
            rows = service.get_fg_warehouse_stock(branch, sorted(codes), warehouse)
        except Exception:
            logger.exception(
                "FG stock lookup failed for branch %s warehouse %s (%d items)",
                branch,
                warehouse,
                len(codes),
            )
            continue

        stock_map[(branch, warehouse)] = {
            row['ItemCode']: {
                'item_name': row.get('ItemName'),
                'warehouse_stock': _to_number(row.get('OnHand')),
            }
            for row in (rows or [])
            if row.get('ItemCode')
        }

    return stock_map


def fg_stock_for_log(invoice_log, stock_map=None):
    """Per-line warehouse stock for one log, ready to serialize.

    Pass `stock_map` from build_fg_stock_map() on list endpoints; without it the
    lookup is done for this log alone (fine for detail / create responses).
    """
    payload = getattr(invoice_log, 'invoice_payload', None)
    if not extract_fg_item_codes(payload):
        return []

    key = _key_of(invoice_log)
    if stock_map is None:
        stock_map = build_fg_stock_map([invoice_log])
    by_item = (stock_map or {}).get(key) or {}
    warehouse = key[1] if key else None

    result = []
    for line in (payload or {}).get('DocumentLines') or []:
        if not isinstance(line, dict):
            continue
        item_code = line.get('ItemCode')
        if not item_code or not str(item_code).upper().startswith('FG'):
            continue
        item_code = str(item_code)
        stock = by_item.get(item_code) or {}
        result.append({
            'line_num': line.get('LineNum'),
            'item_code': item_code,
            'item_name': stock.get('item_name'),
            'quantity': _to_number(line.get('Quantity')),
            'warehouse_code': warehouse,
            # None (not 0) when the item has no OITW row for this warehouse, so
            # "not stocked here" stays distinguishable from "stocked, empty".
            'warehouse_stock': stock.get('warehouse_stock'),
        })
    return result
