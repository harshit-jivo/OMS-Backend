"""Naming and sizing a synced product, which needs its CATEGORY as well as its code.

`sap_products` holds one row per (item_code, category) — that pair is the
catalogue's own unique key — and the rows behind one code are not variants of a
single product. `FG0000306` is YELLOW MUSTARD OIL 1 LTR, 20 to a carton, under
OIL; GLASS BOTTLE 200 MLS BLUEBERRY, 12 to a carton, under BEVERAGES; and
PUMPKIN SEEDS 400 GM, 1, under MART. 614 of ~1770 codes carry a different name
per category and 192 a different `sal_factor2`.

Matching on the code alone therefore answers with whichever row the database
returns first, and `Product.Meta.ordering = ['item_code']` makes that worse than
it looks: the queryset counts as ordered, so `.first()` never reaches a
primary-key tiebreak and the rows come back in plan order. That is how an OIL
order's free YELLOW MUSTARD OIL was labelled GLASS BOTTLE 200 MLS BLUEBERRY on
the approval screen, and how a carton giveaway could be sized at 12 or 1 pieces
instead of 20.

`invoice.services.item_names` answers the same question for invoice payloads,
where the category comes from the log's branch rather than from an order line.
"""

from collections import namedtuple
from decimal import Decimal, InvalidOperation

from django.db.models.functions import Upper

from .models import Product

# `pack_size` is `sal_factor2` — pieces to a carton. It defaults to 1 rather
# than 0 so a BOX quantity converted through an unknown product ships as
# written instead of collapsing to nothing.
ProductInfo = namedtuple('ProductInfo', ('item_name', 'pack_size'))

ZERO = Decimal('0')
ONE = Decimal('1')


def _pack_size(value):
    try:
        size = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return ONE
    return size if size > ZERO else ONE


def resolve(item_codes, category=''):
    """``{item_code: ProductInfo}`` for these codes, read in `category`.

    One query, keyed by the spelling the caller passed in. Matching is
    case-insensitive, as the per-code lookups this replaced were.

    A code with no row in `category` — and every code when the caller has no
    category to state — falls back to its lowest-pk row. That is the old
    code-only answer, made deterministic instead of plan-ordered: still a guess,
    but the same guess on every server and after every re-sync.
    """
    given = {}
    for code in item_codes or ():
        text = str(code or '').strip()
        if text:
            given.setdefault(text.upper(), text)
    if not given:
        return {}

    wanted = str(category or '').strip().casefold()
    rows = (
        Product.objects
        .annotate(_code=Upper('item_code'))
        .filter(_code__in=given)
        # Overrides Meta.ordering, which orders by the very column these rows
        # share — see the module docstring.
        .order_by('pk')
        .values_list('_code', 'category', 'item_name', 'sal_factor2')
    )

    resolved = {}
    fallback = {}
    for code, row_category, item_name, factor in rows:
        info = ProductInfo(str(item_name or '').strip(), _pack_size(factor))
        matches = wanted and str(row_category or '').strip().casefold() == wanted
        (resolved if matches else fallback).setdefault(given[code], info)

    for item_code, info in fallback.items():
        resolved.setdefault(item_code, info)
    return resolved


def name(item_code, category=''):
    """The product's name in `category`, or '' when the code is unknown."""
    info = resolve([item_code], category).get(str(item_code or '').strip())
    return info.item_name if info else ''
