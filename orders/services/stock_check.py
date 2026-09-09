"""Pre-order stock check: is there enough on hand to place this order?

Lifted out of `orders/views.py` (plan item 3.2). The rules are small but
fiddly — what counts as the required quantity for a line, how a line is keyed
against live stock (item code AND category, because the same code exists in
more than one company database), and how a quantity that arrives as '', None
or a string is coerced.

The live-stock lookup itself talks to HANA through `hana.services`; nothing
here builds a response.
"""
from sap_sync.services.connection import SAPConnection




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
