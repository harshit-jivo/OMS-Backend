"""Party-product assignment, many parties at a time.

`assignments.py` answers "what does this party buy, and at what rate" one party
at a time. The job that actually comes up is the other shape: "Haryana's mustard
goes up Rs 4 on Monday" is one item across forty parties, and through the
single-party screen that is forty selections and forty rate dialogs with nothing
at the end confirming the forty agree.

Four endpoints, all taking the same `party_selections` list:

    bulk-party/products/        what the selected parties hold, and at what spread
    bulk-party/update-rates/    re-price across them
    bulk-party/set-active/      turn assignments off or on across them
    bulk-party/copy-catalogue/  give these parties the catalogue that one party has

The first two are not new API design: `Frontend/src/pages/partyProducts/
BulkRateEditor.tsx` and its test file already specify their request and response
shapes exactly, and have done since before this module existed — the component
is rendered whenever more than one party is selected and has been calling two
routes that were never built. Their response shape is therefore fixed, not ours
to choose; the two new endpoints below follow the same shape for consistency.

Three things here differ from `assignments.py` on purpose, all of them fixing
something that module gets wrong:

* **Every write is transactional.** Nothing in `assignments.py` is, so a 400-row
  run that fails halfway leaves the first half committed. These use
  `audited_atomic` so the audit trail rolls back with the rows.
* **Rows are saved one at a time, never `queryset.update()`.** Audit signals fire
  on `.save()` and not on `.update()`. That is not theoretical: `ComboMappingsView
  .post` is the one write path in `assignments.py` using `.update()`, and it
  writes no usable audit row at all. A mass rate change is exactly the thing that
  must stay reviewable afterwards. The cost is bounded by skipping no-ops — the
  documented workflow fills every row with the rate most parties already use, so
  most rows have nothing to write.
* **Partial failure is reported.** `BulkAssignPartyToProductView` hardcodes
  `'success': True`, returns 200 however many rows failed, and silently
  `continue`s past a category mismatch — so an import can add nothing and still
  show zero errors. Here `success` follows the error list, a run that wrote
  nothing at all is a 400, and every skipped row says why.
"""
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation

from django.db.models import Q
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import HasKey
from audit.middleware import mark_exempt
from audit.transactions import audited_atomic
from sap_sync.models import Party, Product, active_product_q
from users.models import PartyProductAssignment

from ._shared import _normalize_category
from .assignments import _normalize_party_selections

#: `basic_rate` is numeric(12,4). Quantizing to the column keeps a percentage
#: change from proposing a rate the database will round behind our back.
RATE_STEP = Decimal('0.0001')
RATE_CEILING = Decimal('99999999.9999')

RATE_MODES = ('set', 'percent', 'amount')
APPLY_TO = ('existing', 'all')

# A runaway selection should not sit on locks over a fifth of the table.
# MAX_TARGET_ROWS is the bound that matters for a write: it caps the rows a run
# may lock however the parties were chosen. MAX_SELECTIONS only bounds the LIST
# a client may post, and it has to admit a real one — the Product Rates page
# sends every party a filter matches, and one salesperson's book alone is 531
# parties. The whole party table is under a thousand rows.
MAX_SELECTIONS = 1000
MAX_ITEMS = 500
MAX_TARGET_ROWS = 2000


def _revised_rate(base, mode, value):
    """The rate a base rate lands on.

    Mirrors `applyMode` in `BulkRateEditor.tsx` — the preview the user confirmed
    is computed there, so the two must agree to the last paisa or the dialog
    lies. All-Decimal: a float here would make 107 * 1.05 land on 112.35000000001
    and write a rate nobody typed.
    """
    if mode == 'set':
        revised = value
    elif mode == 'percent':
        revised = base * (Decimal(1) + value / Decimal(100))
    else:
        revised = base + value
    return revised.quantize(RATE_STEP)


def _to_decimal(raw):
    if raw in (None, ''):
        return None
    try:
        return Decimal(str(raw).replace(',', ''))
    except (TypeError, ValueError, ArithmeticError, InvalidOperation):
        return None


def _eligible_card_codes(selections, category):
    """Selected parties that could hold an item in `category`.

    A selection carrying no category reaches every item; one carrying a category
    reaches only its own, because an OIL item cannot reach a MART party. This is
    the same rule `BulkAssignPartyToProductView` applies when it skips a
    mismatched pair — stated once here so the count a preview shows and the rows
    a write touches cannot drift apart.
    """
    return {code for code, cat in selections if cat is None or cat == category}


def _selection_index(selections):
    """`(any_category_codes, codes_by_category)` for O(1) membership tests."""
    any_category = {code for code, cat in selections if cat is None}
    by_category = defaultdict(set)
    for code, cat in selections:
        if cat:
            by_category[cat].add(code)
    return any_category, by_category


def _selected(index, card_code, category):
    any_category, by_category = index
    return card_code in any_category or card_code in by_category.get(category, ())


def _read_selections(request):
    return _normalize_party_selections(
        request.data.get('party_selections'), request.data.get('card_codes'))


#: `party_scope` value asking the server to find the parties itself.
PARTY_SCOPE_ALL_HOLDERS = 'all_holders'


def _holders_of(items):
    """Every `(card_code, category)` currently assigned one of these items.

    The product-first screen's "turn this off for every party" cannot send a
    party list: one item is held by up to 522 parties here, well past
    MAX_SELECTIONS, and a list built when the page loaded would miss a party
    assigned between then and the click. Resolving it here instead keeps the
    request small and settles the membership question inside the same
    transaction that acts on it.

    Deactivated rows are included on purpose — reactivating is the mirror verb
    and needs exactly the rows a deactivation left behind. The per-row loop
    already counts a row that is nothing to do as `unchanged`.
    """
    query = Q()
    for item_code, category, *_rest in items:
        query |= Q(item_code=item_code, category=category)
    if not query:
        return set()
    return set(
        PartyProductAssignment.objects.filter(query)
        .values_list('card_code', 'category')
    )


def _resolve_targets(request, items):
    """`(selections, error)` — an explicit party list, or the ones holding `items`.

    `party_scope` wins when given, so a client cannot send both and be surprised
    by which one was honoured.
    """
    scope = str(request.data.get('party_scope') or '').strip().lower()
    if scope:
        if scope != PARTY_SCOPE_ALL_HOLDERS:
            return set(), f"party_scope must be '{PARTY_SCOPE_ALL_HOLDERS}'"
        holders = _holders_of(items)
        if not holders:
            return set(), 'No party is assigned any of these products'
        return holders, None
    selections = _read_selections(request)
    if not selections:
        return set(), 'party selection is required'
    return selections, None


def _read_items(raw_items):
    """`[{item_code, category, basic_rate}]` -> `[(item_code, category, value)]`.

    Returns `(items, errors)`. A malformed entry is named rather than dropped
    silently.
    """
    items, errors = [], []
    if not isinstance(raw_items, list):
        return items, ['items must be a list']
    for entry in raw_items:
        if not isinstance(entry, dict):
            errors.append('items: each entry must be an object')
            continue
        item_code = str(entry.get('item_code') or '').strip()
        category = _normalize_category(entry.get('category'))
        if not item_code or not category:
            errors.append(f"{item_code or '(no item code)'}: item_code and category are required")
            continue
        items.append((item_code, category, _to_decimal(entry.get('basic_rate'))))
    return items, errors


def _active_products(item_codes):
    """`{(item_code, category): Product}` for the active products among these."""
    return {
        (product.item_code, product.category): product
        for product in Product.objects.filter(
            active_product_q(), item_code__in=item_codes)
    }


def _known_party_codes(card_codes):
    return set(Party.objects.filter(
        card_code__in=card_codes).values_list('card_code', flat=True))


def _lock_assignments(card_codes, item_codes):
    """The rows a run may touch, locked in primary-key order.

    `order_by('pk')` is load-bearing rather than tidy: two concurrent bulk edits
    over overlapping parties that take their locks in different orders deadlock,
    and it would happen rarely enough to be blamed on the network.
    """
    return list(
        PartyProductAssignment.objects
        .select_for_update()
        .filter(card_code__in=card_codes, item_code__in=item_codes)
        .order_by('pk')
    )


def _bulk_response(payload, errors, wrote_anything):
    """200 when something landed, 400 when nothing did.

    The middle case is the one that matters and the one `assignments.py` gets
    wrong: some rows applied and some refused is a 200 carrying `success: false`
    and the full error list, so the client's result dialog opens and names every
    failure instead of a toast claiming the lot succeeded.
    """
    payload['errors'] = errors
    body = {'success': not errors, 'data': payload}
    if errors and not wrote_anything:
        body['message'] = errors[0]
        return Response(body, status=status.HTTP_400_BAD_REQUEST)
    return Response(body, status=status.HTTP_200_OK)


def _reject(message):
    return Response({'success': False, 'message': message},
                    status=status.HTTP_400_BAD_REQUEST)


def _check_caps(selections, items, cap_selections=True):
    # `cap_selections` is off for a server-resolved scope: the cap exists to stop
    # a client posting an unbounded list, and there is no client list to bound
    # when we found the parties ourselves. MAX_TARGET_ROWS still bounds the work.
    if cap_selections and len(selections) > MAX_SELECTIONS:
        return f'Too many parties selected ({len(selections)}); the limit is {MAX_SELECTIONS}.'
    if len(items) > MAX_ITEMS:
        return f'Too many items ({len(items)}); the limit is {MAX_ITEMS}.'
    reach = sum(len(_eligible_card_codes(selections, category))
                for _code, category, *_rest in items)
    if reach > MAX_TARGET_ROWS:
        return (f'That would touch {reach} assignments; the limit is {MAX_TARGET_ROWS}. '
                f'Narrow the parties or the items.')
    return None


class BulkPartyProductsView(APIView):
    """What the selected parties hold between them, and at what spread.

    Reads the other way round from `PartyProductsView`: one row per distinct
    product rather than per assignment, carrying how many of the selection hold
    it and how far their rates have drifted apart — which is the thing you most
    need to see before overwriting them.

    Two queries regardless of how many parties are selected. `PartyProductsView`
    runs one product lookup per assignment; that N+1 is not copied here, and
    `test_products_is_two_queries` is the guard.
    """

    def get_permissions(self):
        # Same gate upstream's single-party views carry: opening the page is a
        # grant, and every write behind it re-checks. Bulk re-pricing must not
        # be the one door that only asks for a login.
        return [IsAuthenticated(), HasKey('Party_Product_Assignment')]

    def post(self, request):
        # A POST because the question is a list of party selections, not because
        # anything changes. Without this the audit middleware's fallback would
        # file a contentless row per fetch -- see `AuditMiddleware._maybe_fallback`.
        mark_exempt(request)

        selections = _read_selections(request)
        if not selections:
            return _reject('party selection is required')
        if len(selections) > MAX_SELECTIONS:
            return _reject(
                f'Too many parties selected ({len(selections)}); the limit is {MAX_SELECTIONS}.')

        card_codes = {code for code, _cat in selections}
        index = _selection_index(selections)

        # Turned-off rows are normally left out: a deactivated assignment cannot
        # be sold, so it is not part of "what these parties hold". The Product
        # Rates page asks for them, because turning a product back ON for a set
        # of parties needs to see where it is off — a row absent from this list
        # reads as never assigned, which is the wrong thing to act on.
        include_inactive = bool(request.data.get('include_inactive'))
        assignments = PartyProductAssignment.objects.filter(card_code__in=card_codes)
        if not include_inactive:
            assignments = assignments.filter(is_active=True)
        assignments = assignments.values(
            'card_code', 'item_code', 'category', 'basic_rate', 'is_active')

        held = defaultdict(list)
        for row in assignments:
            if _selected(index, row['card_code'], row['category']):
                held[(row['item_code'], row['category'])].append(
                    (row['card_code'], row['basic_rate'], row['is_active']))

        products = _active_products({item for item, _cat in held})

        rows = []
        for (item_code, category), entries in held.items():
            product = products.get((item_code, category))
            if product is None:
                # The assignment outlived its product. `PartyProductsView` drops
                # these too; showing a row that cannot be priced is worse than
                # showing nothing.
                continue
            # Rate statistics describe the SELLABLE rows. A turned-off row keeps
            # the rate it had, but that rate is not what anyone is being charged,
            # so it must not pull the spread or the common rate. Only when
            # nothing is sellable do the kept rates stand in, so that a product
            # turned off everywhere still shows what it was priced at.
            active_rates = [rate for _code, rate, active in entries if active]
            rates = active_rates or [rate for _code, rate, _active in entries]
            eligible = _eligible_card_codes(selections, category)
            party_count = len({code for code, _rate, _active in entries})
            # Shared with the product-first view so the two cannot disagree
            # about the same product's commonest rate.
            common_rate, common_parties = _modal_rate(rates)
            rows.append({
                'item_code': item_code,
                'item_name': product.item_name,
                'category': category,
                'brand': product.brand,
                'variety': product.sub_group,
                'sub_group': product.sub_group,
                'sal_pack_unit': product.sal_pack_unit,
                'party_count': party_count,
                'active_count': len(active_rates),
                'inactive_count': party_count - len(active_rates),
                'eligible_parties': len(eligible),
                'missing_parties': max(len(eligible) - party_count, 0),
                # WHICH parties, not just how many. The counts alone answer
                # "is this consistent"; they cannot answer "consistent for
                # whom", which is what you need before overwriting a rate.
                #
                # Codes only, no names: the caller already has the party list
                # it built the selection from, and joining `Party` here would
                # cost a third query for a name the client can supply — see
                # this view's two-query guarantee and `test_products_is_two_
                # queries`.
                'holders': [
                    {'card_code': code, 'basic_rate': float(rate), 'is_active': active}
                    for code, rate, active in sorted(entries)
                ],
                'missing': sorted(set(eligible) - {code for code, _rate, _active in entries}),
                'min_rate': float(min(rates)),
                'max_rate': float(max(rates)),
                'distinct_rates': len(set(rates)),
                'common_rate': float(common_rate),
                'common_rate_parties': common_parties,
            })

        rows.sort(key=lambda row: (row['item_name'] or '', row['item_code'], row['category']))
        return Response({
            'success': True,
            'data': {
                'products': rows,
                'parties': len(card_codes),
                'total_products': len(rows),
            },
        })


def _modal_rate(rates):
    """The rate most rows are on, ties broken by the LOWEST.

    Shared by both directions of the same question deliberately: the product-first
    and party-first screens show a `common_rate` for the same product, and two
    copies of this rule would eventually disagree about what it is. A tie must
    never silently propose raising a price.
    """
    counts = Counter(rates)
    return min(counts.items(), key=lambda pair: (-pair[1], pair[0]))


class BulkProductPartiesView(APIView):
    """Which parties hold these products, and at what rate — the other direction.

    `BulkPartyProductsView` reads down: pick parties, get the products they hold
    between them. This reads across: pick products, get every party holding each
    one. It is the question behind "who is still on the old mustard price" and
    behind turning a product off everywhere, and nothing in the app answered it.

    Inactive assignments are INCLUDED and flagged. Everywhere else they are
    filtered out because a deactivated row cannot be sold; here they are the
    subject — reactivating needs to see what was turned off, and a party missing
    from the list because it was deactivated would read as never assigned.
    """

    def get_permissions(self):
        # Same gate upstream's single-party views carry: opening the page is a
        # grant, and every write behind it re-checks. Bulk re-pricing must not
        # be the one door that only asks for a login.
        return [IsAuthenticated(), HasKey('Party_Product_Assignment')]

    def post(self, request):
        # Read-only, POST only because the query is a list of items.
        mark_exempt(request)

        items, errors = _read_items(request.data.get('items'))
        if not items:
            return _reject(errors[0] if errors else 'items is required')
        if len(items) > MAX_ITEMS:
            return _reject(f'Too many items ({len(items)}); the limit is {MAX_ITEMS}.')

        wanted = {(code, category) for code, category, *_rest in items}
        products = _active_products({code for code, _cat in wanted})

        assignments = PartyProductAssignment.objects.filter(
            item_code__in={code for code, _cat in wanted},
        ).values('card_code', 'item_code', 'category', 'basic_rate', 'is_active')

        held = defaultdict(list)
        for row in assignments:
            key = (row['item_code'], row['category'])
            if key in wanted:
                held[key].append(row)

        party_names = {
            (party.card_code, party.category): party
            for party in Party.objects.filter(
                card_code__in={row['card_code']
                               for rows in held.values() for row in rows})
        }

        payload = []
        for item_code, category in sorted(wanted):
            product = products.get((item_code, category))
            rows = held.get((item_code, category), [])
            parties = []
            for row in rows:
                party = (party_names.get((row['card_code'], category))
                         or party_names.get((row['card_code'], None)))
                parties.append({
                    'card_code': row['card_code'],
                    'card_name': getattr(party, 'card_name', row['card_code']),
                    'state': getattr(party, 'state', None),
                    'main_group': getattr(party, 'main_group', None),
                    'category': row['category'],
                    'basic_rate': float(row['basic_rate']),
                    'is_active': row['is_active'],
                })
            parties.sort(key=lambda entry: (entry['card_name'] or '', entry['card_code']))

            active_rates = [float(row['basic_rate']) for row in rows if row['is_active']]
            common_rate, common_parties = (
                _modal_rate(active_rates) if active_rates else (0.0, 0))
            payload.append({
                'item_code': item_code,
                'item_name': getattr(product, 'item_name', item_code),
                'category': category,
                'brand': getattr(product, 'brand', None),
                'variety': getattr(product, 'sub_group', None),
                'sub_group': getattr(product, 'sub_group', None),
                'sal_pack_unit': getattr(product, 'sal_pack_unit', None),
                # A product whose SAP row went inactive still has assignments
                # hanging off it, and turning those off is exactly what someone
                # would come here to do — so it is reported, not dropped.
                'product_active': product is not None,
                'party_count': len(rows),
                'active_count': len(active_rates),
                'inactive_count': len(rows) - len(active_rates),
                'min_rate': min(active_rates) if active_rates else 0.0,
                'max_rate': max(active_rates) if active_rates else 0.0,
                'distinct_rates': len(set(active_rates)),
                'common_rate': common_rate,
                'common_rate_parties': common_parties,
                'parties': parties,
            })

        return Response({'success': True, 'data': {'items': payload}})


class BulkPartyRateUpdateView(APIView):
    """Apply a rate revision across the selected parties.

    `rate_mode` decides what the figure means: `set` writes it, `percent` and
    `amount` move each party's OWN rate, which is the whole point — forty parties
    on forty different rates all going up 5% end on forty different rates, not
    one.

    `apply_to='all'` also reaches parties that do not hold the item yet. Only
    `set` can do that: there is no existing rate for a percentage to move.
    """

    def get_permissions(self):
        # Same gate upstream's single-party views carry: opening the page is a
        # grant, and every write behind it re-checks. Bulk re-pricing must not
        # be the one door that only asks for a login.
        return [IsAuthenticated(), HasKey('Party_Product_Assignment')]

    def post(self, request):
        rate_mode = str(request.data.get('rate_mode') or 'set').strip().lower()
        if rate_mode not in RATE_MODES:
            return _reject(f"rate_mode must be one of {', '.join(RATE_MODES)}")

        apply_to = str(request.data.get('apply_to') or 'existing').strip().lower()
        if apply_to not in APPLY_TO:
            return _reject(f"apply_to must be one of {', '.join(APPLY_TO)}")

        # Not downgraded to 'existing' silently. Either the client is right and
        # this combination is impossible, or it is buggy — and quietly assigning
        # a product to forty parties reads ever after like a deliberate decision.
        if apply_to == 'all' and rate_mode != 'set':
            return _reject("apply_to='all' needs rate_mode='set': a percentage or "
                           "amount has no existing rate to move.")

        items, errors = _read_items(request.data.get('items'))
        if not items:
            return _reject(errors[0] if errors else 'items is required')

        # Items first: `party_scope='all_holders'` derives the parties FROM them.
        scoped = bool(str(request.data.get('party_scope') or '').strip())
        selections, problem = _resolve_targets(request, items)
        if problem:
            return _reject(problem)
        if scoped and apply_to == 'all':
            # 'all_holders' means the parties that HAVE it; 'all' means reach the
            # ones that do not. Together they cancel out, and a client asking for
            # both has not decided what it wants.
            return _reject("apply_to='all' cannot be combined with "
                           "party_scope='all_holders'")

        cap = _check_caps(selections, items, cap_selections=not scoped)
        if cap:
            return _reject(cap)

        for item_code, category, value in items:
            if value is None:
                errors.append(f'{item_code}|{category}: basic_rate must be a number')
        items = [entry for entry in items if entry[2] is not None]
        if not items:
            return _reject(errors[0] if errors else 'items is required')

        dry_run = bool(request.data.get('dry_run'))

        products = _active_products({code for code, _cat, _val in items})
        known_parties = _known_party_codes({code for code, _cat in selections})

        counts = {'updated': 0, 'created': 0, 'unchanged': 0, 'skipped': 0}
        touched_parties, touched_items = set(), set()

        with audited_atomic():
            existing = {
                (row.card_code, row.item_code, row.category): row
                for row in _lock_assignments(
                    {code for code, _cat in selections},
                    {code for code, _cat, _val in items})
            }

            for item_code, category, value in items:
                if (item_code, category) not in products:
                    # One line for the item, not one per party: forty identical
                    # lines would push every real failure out of the dialog.
                    reach = len(_eligible_card_codes(selections, category))
                    counts['skipped'] += reach
                    errors.append(f'{item_code}|{category}: not found or inactive '
                                  f'— {reach} parties skipped')
                    continue

                for card_code in sorted(_eligible_card_codes(selections, category)):
                    if card_code not in known_parties:
                        counts['skipped'] += 1
                        errors.append(f'{card_code}: party not found')
                        continue

                    row = existing.get((card_code, item_code, category))
                    base = row.basic_rate if row is not None else None

                    if row is not None and row.is_active:
                        revised = _revised_rate(base, rate_mode, value)
                        if not (Decimal(0) <= revised <= RATE_CEILING):
                            counts['skipped'] += 1
                            errors.append(
                                f'{card_code}: {item_code}|{category} would become '
                                f'{revised}, which is not a usable rate')
                            continue
                        if revised == row.basic_rate:
                            counts['unchanged'] += 1
                            continue
                        if not dry_run:
                            row.basic_rate = revised
                            row.assigned_by = request.user
                            # `auto_now` fields are NOT added to update_fields by
                            # Django; leaving it out would freeze the only
                            # wall-clock evidence of when this landed, which the
                            # Distributor page reads to gate ordering.
                            row.save(update_fields=[
                                'basic_rate', 'assigned_by', 'updated_at'])
                        counts['updated'] += 1
                        touched_parties.add(card_code)
                        touched_items.add((item_code, category))
                        continue

                    if apply_to != 'all':
                        counts['skipped'] += 1
                        continue

                    # apply_to='all', so rate_mode is 'set' and `value` is the rate.
                    if not (Decimal(0) <= value <= RATE_CEILING):
                        counts['skipped'] += 1
                        errors.append(f'{item_code}|{category}: {value} is not a usable rate')
                        continue
                    if not dry_run:
                        if row is not None:
                            # Reviving a deactivated row rather than creating one:
                            # `unique_together` means there is only ever one row
                            # per (party, item, category).
                            row.basic_rate = value
                            row.is_active = True
                            row.assigned_by = request.user
                            row.save(update_fields=[
                                'basic_rate', 'is_active', 'assigned_by', 'updated_at'])
                        else:
                            PartyProductAssignment.objects.update_or_create(
                                card_code=card_code, item_code=item_code, category=category,
                                defaults={'basic_rate': value, 'is_active': True,
                                          'assigned_by': request.user},
                            )
                    counts['created'] += 1
                    touched_parties.add(card_code)
                    touched_items.add((item_code, category))

        payload = dict(counts)
        payload.update({'parties': len(touched_parties), 'items': len(touched_items),
                        'dry_run': dry_run})
        return _bulk_response(
            payload, errors,
            counts['updated'] + counts['created'] + counts['unchanged'] > 0)


class BulkPartyActivationView(APIView):
    """Turn assignments off, or back on, across the selected parties.

    Deactivation is a soft delete here as it is everywhere else in this model —
    every read path filters `is_active=True`, and the row keeps the rate that was
    negotiated for it, so turning one back on restores that rate rather than
    asking for it again.

    This endpoint never creates. A party that does not hold the item is `missing`,
    not an assignment waiting to happen; `bulk-party/assign-products/` is the
    endpoint that assigns.
    """

    def get_permissions(self):
        # Same gate upstream's single-party views carry: opening the page is a
        # grant, and every write behind it re-checks. Bulk re-pricing must not
        # be the one door that only asks for a login.
        return [IsAuthenticated(), HasKey('Party_Product_Assignment')]

    def post(self, request):
        if 'is_active' not in request.data:
            return _reject('is_active is required')
        is_active = bool(request.data.get('is_active'))

        # Items first: `party_scope='all_holders'` derives the parties FROM them.
        items, errors = _read_items(request.data.get('items'))
        if not items:
            return _reject(errors[0] if errors else 'items is required')

        scoped = bool(str(request.data.get('party_scope') or '').strip())
        selections, problem = _resolve_targets(request, items)
        if problem:
            return _reject(problem)

        cap = _check_caps(selections, items, cap_selections=not scoped)
        if cap:
            return _reject(cap)

        dry_run = bool(request.data.get('dry_run'))
        counts = {'updated': 0, 'unchanged': 0, 'missing': 0}
        touched_parties, touched_items = set(), set()

        with audited_atomic():
            existing = {
                (row.card_code, row.item_code, row.category): row
                for row in _lock_assignments(
                    {code for code, _cat in selections},
                    {code for code, _cat, _val in items})
            }

            for item_code, category, _value in items:
                for card_code in sorted(_eligible_card_codes(selections, category)):
                    row = existing.get((card_code, item_code, category))
                    if row is None:
                        counts['missing'] += 1
                        errors.append(
                            f'{card_code}: {item_code}|{category} is not assigned')
                        continue
                    if row.is_active == is_active:
                        counts['unchanged'] += 1
                        continue
                    if not dry_run:
                        row.is_active = is_active
                        row.assigned_by = request.user
                        row.save(update_fields=['is_active', 'assigned_by', 'updated_at'])
                    counts['updated'] += 1
                    touched_parties.add(card_code)
                    touched_items.add((item_code, category))

        payload = dict(counts)
        payload.update({'parties': len(touched_parties), 'items': len(touched_items),
                        'is_active': is_active, 'dry_run': dry_run})
        return _bulk_response(
            payload, errors, counts['updated'] + counts['unchanged'] > 0)


class BulkPartyCopyCatalogueView(APIView):
    """Give the selected parties the catalogue one party already has.

    `overwrite_existing=false` is the mode that earns its place: same items, but
    a target that already holds one keeps the rate negotiated with it. Copying a
    catalogue is usually about coverage, not about imposing one party's prices on
    another.

    Combo mapping (`free_item_code`, `free_qty_per_unit`, `parent_item_code`) is
    copied onto rows this creates, because that mapping is item-global — the
    Combo Mapping page writes it to every party holding the combo — so a new row
    without it would be the odd one out. It is NOT written over an existing row,
    which would let this endpoint silently undo that page's work.

    `scheme` and `is_scheme` are deliberately not copied: `SchemeProduct` is
    state-scoped, so a scheme carried to a party in another state would be one
    that does not apply there.
    """

    def get_permissions(self):
        # Same gate upstream's single-party views carry: opening the page is a
        # grant, and every write behind it re-checks. Bulk re-pricing must not
        # be the one door that only asks for a login.
        return [IsAuthenticated(), HasKey('Party_Product_Assignment')]

    def post(self, request):
        raw_source = request.data.get('source')
        if not isinstance(raw_source, dict):
            return _reject('source is required')
        source_code = str(raw_source.get('card_code') or '').strip()
        source_category = _normalize_category(raw_source.get('category'))
        if not source_code:
            return _reject('source.card_code is required')

        targets = _read_selections(request)
        if not targets:
            return _reject('party selection is required')
        if len(targets) > MAX_SELECTIONS:
            return _reject(
                f'Too many parties selected ({len(targets)}); the limit is {MAX_SELECTIONS}.')

        overwrite = request.data.get('overwrite_existing')
        overwrite = True if overwrite is None else bool(overwrite)
        dry_run = bool(request.data.get('dry_run'))

        source_rows = PartyProductAssignment.objects.filter(
            card_code=source_code, is_active=True)
        if source_category:
            source_rows = source_rows.filter(category=source_category)
        source_rows = list(source_rows)
        if not source_rows:
            return _reject(f'{source_code} has no active assignments to copy')

        errors = []
        cap = _check_caps(targets, [(row.item_code, row.category, None) for row in source_rows])
        if cap:
            return _reject(cap)

        products = _active_products({row.item_code for row in source_rows})
        known_parties = _known_party_codes({code for code, _cat in targets})

        counts = {'created': 0, 'updated': 0, 'unchanged': 0, 'skipped': 0}
        touched_parties, touched_items = set(), set()

        with audited_atomic():
            existing = {
                (row.card_code, row.item_code, row.category): row
                for row in _lock_assignments(
                    {code for code, _cat in targets},
                    {row.item_code for row in source_rows})
            }

            for source_row in source_rows:
                item_code, category = source_row.item_code, source_row.category
                if (item_code, category) not in products:
                    reach = len(_eligible_card_codes(targets, category))
                    counts['skipped'] += reach
                    errors.append(f'{item_code}|{category}: not found or inactive '
                                  f'— {reach} parties skipped')
                    continue

                for card_code in sorted(_eligible_card_codes(targets, category)):
                    if card_code == source_code:
                        counts['skipped'] += 1
                        continue
                    if card_code not in known_parties:
                        counts['skipped'] += 1
                        errors.append(f'{card_code}: party not found')
                        continue

                    row = existing.get((card_code, item_code, category))
                    if row is not None and row.is_active and not overwrite:
                        counts['unchanged'] += 1
                        continue

                    if row is not None:
                        if row.is_active and row.basic_rate == source_row.basic_rate:
                            counts['unchanged'] += 1
                            continue
                        if not dry_run:
                            row.basic_rate = source_row.basic_rate
                            row.is_active = True
                            row.assigned_by = request.user
                            row.save(update_fields=[
                                'basic_rate', 'is_active', 'assigned_by', 'updated_at'])
                        counts['updated'] += 1
                    else:
                        if not dry_run:
                            PartyProductAssignment.objects.update_or_create(
                                card_code=card_code, item_code=item_code, category=category,
                                defaults={
                                    'basic_rate': source_row.basic_rate,
                                    'is_active': True,
                                    'assigned_by': request.user,
                                    'parent_item_code': source_row.parent_item_code,
                                    'free_item_code': source_row.free_item_code,
                                    'free_qty_per_unit': source_row.free_qty_per_unit,
                                },
                            )
                        counts['created'] += 1
                    touched_parties.add(card_code)
                    touched_items.add((item_code, category))

        payload = dict(counts)
        payload.update({'parties': len(touched_parties), 'items': len(touched_items),
                        'source': source_code, 'dry_run': dry_run})
        return _bulk_response(
            payload, errors,
            counts['created'] + counts['updated'] + counts['unchanged'] > 0)
