"""Product names for the item codes on an invoice payload.

An invoice payload stores what SAP needs — `ItemCode` — but a reviewer reads
product names, not FG numbers. The names come from the synced `sap_sync.Product`
catalogue rather than HANA, so the review screen keeps working when HANA is
down, and covers every item code rather than only the FG lines a warehouse
lookup can reach.

**A code alone is not enough to name a product.** The catalogue holds one row per
(item_code, category), and 614 of ~1770 codes carry a *different* name in each:
`CG0000001` is "PREFORM SAMPLE" under OIL, "TAPE ROLL" under MART and "PREFORM
SAMPLE GMS" under BEVERAGES. Matching on the code alone would show whichever row
the database happened to return first — the wrong product, on an audit screen.

The lines themselves carry no category, so the log's `branch` supplies it: it is
the company database the invoice belongs to, and each maps to its own categories.

The payload is never touched: it is the record of what was sent to SAP, and
rewriting it to carry names would corrupt the audit trail. The names travel
beside it instead.
"""

from sap_sync.models import Product

CONTEXT_KEY = 'item_names'

# Logs written before `branch` was populated carry NULL; every one of them is
# from the oil company DB.
DEFAULT_BRANCH = 'OIL'

# Categories reachable from each company database, most likely first. MART sits
# in the oil company alongside OIL — the same split `resolve_company_db_for_order`
# makes — so an oil invoice falls back to a MART name only when the code has no
# OIL row at all.
BRANCH_CATEGORIES = {
    'OIL': ('OIL', 'MART'),
    'BEVERAGE': ('BEVERAGES',),
}


def categories_for_branch(branch):
    key = str(branch or DEFAULT_BRANCH).strip().upper()
    return BRANCH_CATEGORIES.get(key, BRANCH_CATEGORIES[DEFAULT_BRANCH])


def _categories_of(invoice_log):
    return categories_for_branch(getattr(invoice_log, 'branch', None))


def extract_item_codes(payload):
    """Every item code on one invoice payload, in the order the lines appear."""
    lines = (payload or {}).get('DocumentLines') or []
    codes = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        code = str(line.get('ItemCode') or '').strip()
        if code and code not in codes:
            codes.append(code)
    return codes


def build_item_name_map(invoice_logs):
    """{(category, item_code): item_name} across these logs — one query.

    Keyed by category as well as code, because the same code names a different
    product in each. Callers resolve a log's lines through
    :func:`item_names_for_log`, which knows the branch's category order.
    """
    codes = set()
    categories = set()
    for log in invoice_logs:
        log_codes = extract_item_codes(getattr(log, 'invoice_payload', None))
        if not log_codes:
            continue
        codes.update(log_codes)
        categories.update(_categories_of(log))

    if not codes:
        return {}

    rows = (
        Product.objects
        .filter(item_code__in=codes, category__in=categories)
        .values_list('category', 'item_code', 'item_name')
    )

    names = {}
    for category, item_code, item_name in rows:
        name = str(item_name or '').strip()
        if not name:
            continue
        key = (str(category or '').strip().upper(), item_code)
        names.setdefault(key, name)
    return names


def item_names_for_log(invoice_log, name_map=None):
    """{item_code: item_name} for one log's lines, resolved in its own branch.

    Pass `name_map` from build_item_name_map() on list endpoints; without it the
    lookup is done for this log alone (fine for detail / create responses).
    """
    codes = extract_item_codes(getattr(invoice_log, 'invoice_payload', None))
    if not codes:
        return {}
    if name_map is None:
        name_map = build_item_name_map([invoice_log])

    categories = _categories_of(invoice_log)
    resolved = {}
    for code in codes:
        for category in categories:
            name = name_map.get((category, code))
            if name:
                resolved[code] = name
                break
    return resolved
