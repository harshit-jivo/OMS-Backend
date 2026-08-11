"""Scheme engine v2 — resolve which schemes reach a vendor and how much they give.

The entry point is :func:`resolve_schemes`, a **pure** function: it takes a party,
a category and the order's lines and returns proposals. Nothing is written. That
makes the same code usable from the order-create path, from the Add Sales preview
endpoint (called on every quantity change), and from tests.

Targeting lives entirely in ``SchemeAssignment``: one ``STATE`` row reaches every
vendor in that state, one ``PARTY`` row reaches a single vendor, and the more
specific of the two wins. 1+1 combos are handled by ``SchemeTrigger.applies_to``
— ``FREE_LINE`` takes the qualifying quantity from the auto-added zero-priced
companion line, so the giveaway is computed on top of the combo's free half.

See docs/scheme-architecture.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db.models import Q
from django.utils import timezone

from .models import (
    UOM_FIELDS,
    Scheme,
    SchemeAssignment,
    SchemeBenefit,
    SchemeTrigger,
)
from .scheme_rules import get_party_state_code

ZERO = Decimal('0')


def _to_decimal(value, default=ZERO):
    if value in (None, ''):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _get(line, key, default=None):
    """Read a field off an order line that may be a dict (create payload) or an
    OrderItem instance (re-resolution of a saved order)."""
    if isinstance(line, dict):
        value = line.get(key, default)
    else:
        value = getattr(line, key, default)
    return default if value is None else value


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {'1', 'true', 'yes', 'y'}


def _line_qty(line, uom):
    """Quantity of `line` measured in `uom`.

    Falls back to the plain `qty` column when the requested measure is absent or
    zero — most lines only carry `qty` populated.
    """
    field_name = UOM_FIELDS.get(uom, 'qty')
    value = _to_decimal(_get(line, field_name, 0))
    if value <= ZERO and field_name != 'qty':
        value = _to_decimal(_get(line, 'qty', 0))
    return value


@dataclass
class PartyContext:
    """Everything about the vendor that scoping depends on. Resolved once."""

    card_code: str
    category: str = ''
    state_code: str = ''
    main_group: str = ''


@dataclass
class SchemeProposal:
    """One giveaway the engine proposes for one order line."""

    line_index: int
    trigger_item_code: str
    scheme_id: int
    scheme_code: str
    scheme_name: str
    benefit_id: int
    benefit_item_code: str
    free_uom: str
    qty: Decimal
    qualifying_qty: Decimal
    scope_type: str
    scope_value: str
    priority: int = 0
    stackable: bool = False
    # True when the benefit carries no rule (per_qty and free_qty both 0) and the
    # quantity is still the user's to type. Every migrated legacy scheme is one
    # of these, which is how v2 preserves today's behaviour.
    qty_is_user_supplied: bool = False

    def as_dict(self):
        return {
            'line_index': self.line_index,
            'trigger_item_code': self.trigger_item_code,
            'scheme_id': self.scheme_id,
            'scheme_code': self.scheme_code,
            'scheme_name': self.scheme_name,
            'benefit_id': self.benefit_id,
            'benefit_item_code': self.benefit_item_code,
            'free_uom': self.free_uom,
            'qty': str(self.qty),
            'qualifying_qty': str(self.qualifying_qty),
            'scope_type': self.scope_type,
            'scope_value': self.scope_value,
            'priority': self.priority,
            'stackable': self.stackable,
            'qty_is_user_supplied': self.qty_is_user_supplied,
        }


@dataclass
class _Candidate:
    """A scheme that reaches this party, plus the assignment that let it in."""

    scheme: Scheme
    scope_type: str
    scope_value: str
    specificity: int
    triggers: list = field(default_factory=list)
    benefits: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Step 1 — party context
# ---------------------------------------------------------------------------

def build_party_context(card_code, category=''):
    from django.apps import apps

    main_group = ''
    for app_label, model_name in (('sap_sync', 'Party'), ('orders', 'Parties')):
        try:
            PartyModel = apps.get_model(app_label, model_name)
        except LookupError:
            continue
        value = (
            PartyModel.objects
            .filter(card_code=card_code)
            .exclude(main_group__isnull=True)
            .exclude(main_group__exact='')
            .values_list('main_group', flat=True)
            .first()
        )
        if value:
            main_group = value
            break

    return PartyContext(
        card_code=card_code or '',
        category=category or '',
        state_code=get_party_state_code(card_code) or '',
        main_group=main_group,
    )


# ---------------------------------------------------------------------------
# Steps 2-4 — which schemes reach this party
# ---------------------------------------------------------------------------

def _scope_filter(ctx):
    """OR together every scope this party belongs to.

    Blank context values are left out rather than matched as '' — a party with no
    state must not collect every scheme whose scope_value happens to be empty.
    """
    condition = Q(scope_type=SchemeAssignment.SCOPE_ALL)
    if ctx.card_code:
        condition |= Q(scope_type=SchemeAssignment.SCOPE_PARTY, scope_value=ctx.card_code)
    if ctx.main_group:
        condition |= Q(scope_type=SchemeAssignment.SCOPE_MAIN_GROUP, scope_value__iexact=ctx.main_group)
    if ctx.state_code:
        condition |= Q(scope_type=SchemeAssignment.SCOPE_STATE, scope_value__iexact=ctx.state_code)
    if ctx.category:
        condition |= Q(scope_type=SchemeAssignment.SCOPE_CATEGORY, scope_value__iexact=ctx.category)
    return condition


def get_candidate_schemes(ctx, on_date=None):
    """Return the live schemes reaching this party, most specific assignment kept.

    Exclusions are applied first and are absolute — a carve-out at any scope
    removes the scheme regardless of how specific the granting row was.
    """
    on_date = on_date or timezone.localdate()

    assignments = (
        SchemeAssignment.objects
        .filter(_scope_filter(ctx), is_active=True)
        .filter(Q(category='') | Q(category__iexact=ctx.category or ''))
        .select_related('scheme')
    )

    # A scheme's own category is a hard wall, not a targeting preference: an OIL
    # scheme is not an offer that a MART order can qualify for, no matter which
    # assignment let it in. Only applied when the caller told us the category —
    # with none supplied ("all categories" in the admin view) every scheme is
    # still worth reporting, and `_line_category_allows` catches the rest.
    if ctx.category:
        assignments = assignments.filter(
            Q(scheme__category='') | Q(scheme__category__iexact=ctx.category)
        )

    excluded_scheme_ids = set()
    granting = {}

    for assignment in assignments:
        if not assignment.is_live_on(on_date):
            continue
        if assignment.is_exclusion:
            excluded_scheme_ids.add(assignment.scheme_id)
            continue
        if not assignment.scheme.is_live_on(on_date):
            continue
        current = granting.get(assignment.scheme_id)
        if current is None or assignment.specificity > current.specificity:
            granting[assignment.scheme_id] = assignment

    candidates = {
        scheme_id: _Candidate(
            scheme=assignment.scheme,
            scope_type=assignment.scope_type,
            scope_value=assignment.scope_value,
            specificity=assignment.specificity,
        )
        for scheme_id, assignment in granting.items()
        if scheme_id not in excluded_scheme_ids
    }

    if not candidates:
        return []

    # Two queries for the children rather than N — this runs on every keystroke
    # in the Add Sales preview.
    for trigger in SchemeTrigger.objects.filter(scheme_id__in=candidates):
        candidates[trigger.scheme_id].triggers.append(trigger)
    for benefit in SchemeBenefit.objects.filter(scheme_id__in=candidates):
        candidates[benefit.scheme_id].benefits.append(benefit)

    return [c for c in candidates.values() if c.triggers and c.benefits]


# ---------------------------------------------------------------------------
# Step 5 — trigger matching
# ---------------------------------------------------------------------------

def _line_attribute(line, match_type):
    if match_type == 'ITEM':
        return _get(line, 'item_code', '')
    if match_type == 'SUB_GROUP':
        return _get(line, 'sub_group', '') or _get(line, 'variety', '')
    if match_type == 'VARIETY':
        return _get(line, 'variety', '') or _get(line, 'sub_group', '')
    if match_type == 'BRAND':
        return _get(line, 'brand', '')
    if match_type == 'CATEGORY':
        return _get(line, 'category', '')
    return ''


def _line_category_allows(scheme, line):
    """Whether a categorised scheme may fire on this line.

    The party-level filter uses one category for the whole call, but an order
    can mix them — Add Sales sends the first confirmed row's category. This is
    the per-line guard that stops an OIL scheme reaching the MART lines of a
    mixed order. A line with no category stated is not blocked; only a stated,
    different one is.
    """
    if not scheme.category:
        return True
    line_category = str(_get(line, 'category', '') or '').strip()
    if not line_category:
        return True
    return line_category.casefold() == scheme.category.casefold()


def _trigger_matches(trigger, line):
    if trigger.match_type == 'ALL':
        return True
    actual = str(_line_attribute(line, trigger.match_type) or '').strip()
    return bool(actual) and actual.casefold() == str(trigger.match_value or '').strip().casefold()


def _index_free_lines(lines):
    """Map combo item_code -> its auto-added zero-priced companion lines."""
    index = {}
    for line in lines:
        if not _as_bool(_get(line, 'is_auto_free', False)):
            continue
        source = str(_get(line, 'combo_source_code', '') or '').strip()
        if source:
            index.setdefault(source, []).append(line)
    return index


def _qualifying_qty(trigger, line, free_line_index):
    """Quantity that counts towards the trigger, per its `applies_to`."""
    paid = _line_qty(line, trigger.min_uom)
    if trigger.applies_to == 'PAID_LINE':
        return paid

    companions = free_line_index.get(str(_get(line, 'item_code', '') or '').strip(), [])
    free = sum((_line_qty(companion, trigger.min_uom) for companion in companions), ZERO)

    if trigger.applies_to == 'FREE_LINE':
        return free
    return paid + free


# ---------------------------------------------------------------------------
# Step 6 — benefit quantity
# ---------------------------------------------------------------------------

def compute_benefit_qty(benefit, qualifying_qty):
    """How much `benefit` gives away for `qualifying_qty`.

    Returns ``(qty, qty_is_user_supplied)``. A benefit with no rule at all
    (per_qty and free_qty both 0) proposes nothing and leaves the number to the
    user — that is what every scheme migrated from `scheme_product` looks like,
    so v2 reproduces today's behaviour until a rule is filled in.
    """
    per_qty = _to_decimal(benefit.per_qty)
    free_qty = _to_decimal(benefit.free_qty)

    if per_qty <= ZERO and free_qty <= ZERO:
        return ZERO, True

    if per_qty > ZERO:
        qty = (qualifying_qty // per_qty) * free_qty
    else:
        # Flat giveaway, but still only if something was actually ordered.
        qty = free_qty if qualifying_qty > ZERO else ZERO

    if benefit.max_free_qty is not None:
        qty = min(qty, _to_decimal(benefit.max_free_qty))

    return max(qty, ZERO), False


# ---------------------------------------------------------------------------
# Step 7 — conflicts
# ---------------------------------------------------------------------------

def _resolve_conflicts(proposals):
    """Within one (line, giveaway item), higher priority wins.

    If the winner is not stackable it applies alone. If it is, every stackable
    proposal below it also applies, up to the first non-stackable one.
    """
    grouped = {}
    for proposal in proposals:
        grouped.setdefault((proposal.line_index, proposal.benefit_item_code), []).append(proposal)

    kept = []
    for group in grouped.values():
        group.sort(key=lambda p: (-p.priority, p.scheme_id))
        if not group[0].stackable:
            kept.append(group[0])
            continue
        for proposal in group:
            if not proposal.stackable:
                break
            kept.append(proposal)

    kept.sort(key=lambda p: (p.line_index, -p.priority, p.scheme_id))
    return kept


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def resolve_schemes(card_code, category, lines, on_date=None, ctx=None):
    """Propose scheme giveaways for an order.

    `lines` may be create-payload dicts or saved OrderItem instances. Returns a
    list of :class:`SchemeProposal`, already de-conflicted. Nothing is written.
    """
    on_date = on_date or timezone.localdate()
    ctx = ctx or build_party_context(card_code, category)

    candidates = get_candidate_schemes(ctx, on_date=on_date)
    if not candidates:
        return []

    lines = list(lines or [])
    free_line_index = _index_free_lines(lines)
    proposals = []

    for line_index, line in enumerate(lines):
        # The companion lines are consumed via `applies_to=FREE_LINE` on their
        # combo's trigger; treating them as triggers in their own right would
        # count the same stock twice. SCHEME lines are giveaways already.
        if _as_bool(_get(line, 'is_auto_free', False)):
            continue
        if str(_get(line, 'item_type', '') or '').upper() == 'SCHEME':
            continue

        line_item_code = str(_get(line, 'item_code', '') or '').strip()

        for candidate in candidates:
            if not _line_category_allows(candidate.scheme, line):
                continue
            matched = [t for t in candidate.triggers if _trigger_matches(t, line)]
            if not matched:
                continue

            # A scheme with several triggers hitting one line qualifies once, on
            # whichever trigger yields the most — the offer is per line, not per
            # matching rule.
            best = None
            for trigger in matched:
                qualifying = _qualifying_qty(trigger, line, free_line_index)
                if qualifying < _to_decimal(trigger.min_qty):
                    continue
                if best is None or qualifying > best:
                    best = qualifying
            if best is None:
                continue

            for benefit in candidate.benefits:
                qty, user_supplied = compute_benefit_qty(benefit, best)
                if qty <= ZERO and not user_supplied:
                    continue
                proposals.append(SchemeProposal(
                    line_index=line_index,
                    trigger_item_code=line_item_code,
                    scheme_id=candidate.scheme.id,
                    scheme_code=candidate.scheme.code,
                    scheme_name=candidate.scheme.name,
                    benefit_id=benefit.id,
                    benefit_item_code=benefit.free_item_code or line_item_code,
                    free_uom=benefit.free_uom,
                    qty=qty,
                    qualifying_qty=best,
                    scope_type=candidate.scope_type,
                    scope_value=candidate.scope_value,
                    priority=candidate.scheme.priority,
                    stackable=candidate.scheme.stackable,
                    qty_is_user_supplied=user_supplied,
                ))

    return _resolve_conflicts(proposals)


def applicable_schemes(card_code, category='', on_date=None):
    """Every scheme reaching this vendor, with the scope that let it in.

    Feeds the admin "why does this vendor have this scheme?" view — the first
    question anyone asks about a wrong giveaway.
    """
    ctx = build_party_context(card_code, category)
    rows = []
    for candidate in get_candidate_schemes(ctx, on_date=on_date):
        rows.append({
            'scheme_id': candidate.scheme.id,
            'code': candidate.scheme.code,
            'name': candidate.scheme.name,
            'category': candidate.scheme.category,
            'priority': candidate.scheme.priority,
            'stackable': candidate.scheme.stackable,
            'valid_from': candidate.scheme.valid_from,
            'valid_to': candidate.scheme.valid_to,
            'granted_by': {'scope_type': candidate.scope_type, 'scope_value': candidate.scope_value},
            'triggers': [
                {
                    'match_type': t.match_type,
                    'match_value': t.match_value,
                    'min_qty': str(t.min_qty),
                    'min_uom': t.min_uom,
                    'applies_to': t.applies_to,
                }
                for t in candidate.triggers
            ],
            'benefits': [
                {
                    'id': b.id,
                    'free_item_code': b.free_item_code,
                    'free_uom': b.free_uom,
                    'per_qty': str(b.per_qty),
                    'free_qty': str(b.free_qty),
                    'max_free_qty': str(b.max_free_qty) if b.max_free_qty is not None else None,
                }
                for b in candidate.benefits
            ],
            'context': {
                'card_code': ctx.card_code,
                'state_code': ctx.state_code,
                'main_group': ctx.main_group,
                'category': ctx.category,
            },
        })
    return rows
