# Scheme Architecture (v2)

Design for product schemes that can be targeted at **one vendor** or at **a whole
state of vendors**, and that work for both single-FG items and 1+1 combo packs
(including schemes that ride on top of the combo's *free* half).

---

## 1. Why the current model can't stretch

| Concern | Where it lives today | Problem |
|---|---|---|
| Offer identity | `scheme_product.scheme_name` | Also used as a join key |
| Giveaway item | `scheme_product.item_code` | One row per giveaway item; a multi-item scheme is N rows sharing a name |
| Geography | `scheme_product.state_code` | Only filters the picker — not an application scope |
| Who gets it | `party_product_assignments.scheme_id` | One row per party × item × category. A state-wide scheme = a row per vendor |
| What earns it | implicit (the assignment's `item_code`) | No thresholds, no ratios, no sub-group/brand targeting |
| How much is free | `order_items.qty_scheme`, typed by the user | No rule, no cap, no audit of *why* that number |
| 1+1 combos | `party_product_assignments.free_item_code` + `order_items.is_auto_free` | A parallel mechanism the scheme engine cannot see |

Two concrete defects that fall out of this:

1. **Name-based fan-out.** `sync_service.get_scheme_item_codes_for_combo()` resolves
   a scheme's giveaway items by matching `scheme_name`. Renaming a scheme, or
   creating a second scheme with the same name in another state, changes what SAP
   ships.
2. **No snapshot.** The SAP push re-resolves `item_code` from `scheme_product` at
   push time. Editing a scheme rewrites what an already-approved order will ship.

---

## 2. The four tables

### 2.1 `schemes` — the offer

```python
class Scheme(models.Model):
    code          = CharField(max_length=50, unique=True)   # human key, e.g. "PB-CP1L-Q4"
    name          = CharField(max_length=255)
    description   = TextField(blank=True)

    category      = CharField(choices=['OIL','BEVERAGES','MART'], blank=True)
    # '' => every category. See §2.5.

    valid_from    = DateField(null=True, blank=True)
    valid_to      = DateField(null=True, blank=True)
    is_active     = BooleanField(default=True)

    priority      = IntegerField(default=0)      # higher wins a conflict
    stackable     = BooleanField(default=False)  # may combine with other schemes

    created_by / created_at / updated_at
```

One row per offer. Nothing about geography, products, or vendors lives here.

### 2.2 `scheme_benefits` — what is given away (1..N per scheme)

```python
class SchemeBenefit(models.Model):
    scheme          = FK(Scheme, related_name='benefits')

    free_item_code  = CharField(max_length=50, null=True, blank=True)
    # NULL => "same item as the trigger line" (buy 10 boxes, get 1 box of the same)

    free_uom        = CharField(choices=['QTY','PCS','BOX','LTR'], default='PCS')

    # Ratio rule: buy `per_qty`, get `free_qty`.
    per_qty         = DecimalField(default=0)   # 0 => flat giveaway, not a ratio
    free_qty        = DecimalField(default=0)
    max_free_qty    = DecimalField(null=True, blank=True)   # cap per order line
```

This replaces the "several rows sharing one `scheme_name`" hack with an explicit
child table. `get_scheme_item_codes_for_combo(scheme_id)` collapses into
`scheme.benefits.all()` — no string matching, no rename hazard.

### 2.3 `scheme_triggers` — what earns it (1..N per scheme)

```python
class SchemeTrigger(models.Model):
    scheme       = FK(Scheme, related_name='triggers')

    match_type   = CharField(choices=['ITEM','SUB_GROUP','VARIETY','BRAND','CATEGORY','ALL'])
    match_value  = CharField(max_length=100, blank=True)   # '' when match_type='ALL'

    min_qty      = DecimalField(default=0)                 # threshold to qualify
    min_uom      = CharField(choices=['QTY','PCS','BOX','LTR'], default='QTY')

    applies_to   = CharField(choices=['PAID_LINE','FREE_LINE','BOTH'],
                             default='PAID_LINE')
```

`applies_to` is the **1+1 answer**:

- `PAID_LINE` — the ordered (priced) quantity qualifies. Normal single-FG case.
- `FREE_LINE` — the qualifying quantity is the auto-added zero-priced companion
  line (`OrderItem.is_auto_free=True`, `combo_source_code=<the combo>`). The
  scheme's giveaway is therefore computed *on top of the free item* of a 1+1.
- `BOTH` — the sum of the two.

Combined with `SchemeBenefit.free_item_code`, both readings of "applied on top of
the free item" are expressible:

| Intent | Trigger | Benefit |
|---|---|---|
| Give extra units of the combo's free item | `applies_to=FREE_LINE` | `free_item_code = <the combo's free item>` |
| Give something else, but sized off the free half | `applies_to=FREE_LINE` | `free_item_code = <other SKU>` |
| Ordinary single-FG scheme | `applies_to=PAID_LINE` | `per_qty=10, free_qty=1` |

### 2.4 `scheme_assignments` — who gets it (1..N per scheme)

```python
class SchemeAssignment(models.Model):
    scheme        = FK(Scheme, related_name='assignments')

    scope_type    = CharField(choices=['PARTY','MAIN_GROUP','STATE','CATEGORY','ALL'])
    scope_value   = CharField(max_length=100, blank=True)
    # PARTY      -> parties.card_code
    # STATE      -> users_state.code   (e.g. 'PB')
    # MAIN_GROUP -> parties.main_group
    # CATEGORY   -> OIL / BEVERAGES / MART
    # ALL        -> '' (every vendor)

    category      = CharField(max_length=20, blank=True)   # optional extra narrowing
    is_exclusion  = BooleanField(default=False)            # carve-outs

    valid_from / valid_to    # optional per-assignment override of the scheme window
    is_active     = BooleanField(default=True)

    class Meta:
        unique_together = ('scheme', 'scope_type', 'scope_value', 'category')
        indexes = [Index(fields=['scope_type', 'scope_value', 'is_active'])]
```

This is the requirement, directly:

- **One vendor** → `scope_type='PARTY', scope_value='CUSTA000123'`
- **A whole state** → `scope_type='STATE', scope_value='PB'` — one row covers
  every vendor in Punjab, present and future.
- **A state minus one vendor** → the STATE row plus
  `scope_type='PARTY', scope_value='CUSTA000123', is_exclusion=True`.

Because the same item lives under OIL / BEVERAGES / MART, `category` narrows any
scope without needing a separate scope type.

### 2.5 `Scheme.category` — which business line the offer belongs to

`SchemeAssignment.category` answers "should this *targeting row* be limited to
one category?", which has to be set again on every row and defaults to "all".
That makes the common case — an OIL scheme aimed at an OIL dealer — leak: a
`PARTY` row with a blank category grants the scheme to that vendor's MART and
BEVERAGES orders too.

`Scheme.category` is the offer's own business line, and it is a wall rather than
a preference:

- blank → every category, which is what every scheme predating the field means;
- `OIL` → the scheme is only ever a candidate for OIL, no matter which
  assignment let it in.

It is enforced twice, because an order can mix categories while the engine call
carries only one:

1. **Party level** (`get_candidate_schemes`) — when the caller states a
   category, schemes of a different one are filtered out in SQL.
2. **Line level** (`_line_category_allows`, step 5) — a categorised scheme is
   skipped for any line whose own `category` differs. Add Sales sends the first
   confirmed row's category for the whole call, so this is what stops an OIL
   scheme reaching the MART lines of a mixed order.

A line that states no category is not blocked — only a stated, different one is.

---

## 3. Resolution

New module `orders/scheme_engine.py`.

```python
resolve_schemes(card_code, category, lines, on_date=None) -> list[AppliedScheme]
```

**Step 1 — party context (one query).** Resolve `state_code` via the existing
`scheme_rules.get_party_state_code()`, plus `main_group` from `Parties`.

**Step 2 — candidate assignments (one query).** OR together the scopes the party
belongs to:

```python
Q(scope_type='PARTY',      scope_value=card_code)  |
Q(scope_type='MAIN_GROUP', scope_value=main_group) |
Q(scope_type='STATE',      scope_value=state_code) |
Q(scope_type='CATEGORY',   scope_value=category)   |
Q(scope_type='ALL')
```
filtered by `is_active`, the date window, and `category in ('', category)`.

**Step 3 — exclusions.** Drop any scheme with a matching `is_exclusion=True` row.
Exclusions are absolute at every level, not specificity-ranked — an explicit
carve-out is always intentional.

**Step 4 — specificity.** Keep the single most specific assignment per scheme:

```
PARTY 100  >  MAIN_GROUP 60  >  STATE 50  >  CATEGORY 20  >  ALL 0
```

A per-party row therefore overrides the state-wide default for that vendor.

**Step 5 — trigger match per order line.** Compute qualifying quantity from
`applies_to`:

- `PAID_LINE` → `scheme_rules.get_ordered_quantity(line)`
- `FREE_LINE` → sum of qty over lines where
  `is_auto_free=True and combo_source_code == line.item_code`
- `BOTH` → the sum

Skip the line if qualifying qty < `min_qty`.

**Step 6 — benefit quantity.**

```python
if benefit.per_qty > 0:
    qty = floor(qualifying_qty / benefit.per_qty) * benefit.free_qty
else:
    qty = benefit.free_qty
if benefit.max_free_qty is not None:
    qty = min(qty, benefit.max_free_qty)
```

**Step 7 — conflicts.** Sort by `priority` desc, then scheme id. If the winner is
not `stackable`, keep only it for that (line, benefit item) pair.

The engine is **pure** — it takes lines and returns proposals. That makes it
callable from the order-create path, from a dry-run preview endpoint the Add Sales
screen hits on every quantity change, and from tests, with no duplication.

---

## 4. Persistence

`OrderItemScheme` becomes the record of what was actually granted:

```python
class OrderItemScheme(models.Model):
    order_item        = FK(OrderItem, related_name='schemes')

    scheme            = FK('users.SchemeProduct', null=True)   # legacy, kept for history
    scheme_v2         = FK('orders.Scheme', null=True, on_delete=PROTECT)
    benefit           = FK('orders.SchemeBenefit', null=True, on_delete=SET_NULL)

    benefit_item_code = CharField(max_length=50, null=True)    # SNAPSHOT
    qty_scheme        = DecimalField(...)                      # what was granted
    computed_qty      = DecimalField(default=0)                # what the engine proposed
    is_manual_override= BooleanField(default=False)
    scope_type        = CharField(blank=True)                  # why it applied
    scope_value       = CharField(blank=True)
```

Two things matter here:

- **`benefit_item_code` is a snapshot.** The SAP push reads it directly instead of
  re-resolving from the scheme tables, so editing a scheme can no longer change
  what an already-approved order ships. `sync_service._get_order_item_scheme_entries`
  and `get_scheme_item_codes_for_combo` both collapse to reading this column.
- **`computed_qty` vs `qty_scheme`** keeps the audit trail when a user overrides the
  engine's number, and `scope_type`/`scope_value` record *why* the scheme applied
  ("state PB") — which is the first question anyone asks about a wrong scheme.

---

## 5. Unifying combos (phase 2)

Once the engine is live, the `free_item_code` / `free_qty_per_unit` columns on
`party_product_assignments` become expressible as an ordinary scheme:

```
Scheme(code='COMBO-CP5L-OLIVE1L', priority=1000, stackable=True)
  Trigger(match_type='ITEM', match_value='<combo code>', applies_to='PAID_LINE')
  Benefit(free_item_code='<olive 1L>', per_qty=1, free_qty=1)
  Assignment(scope_type='ALL')
```

That would leave one mechanism instead of two. It is deliberately **not** part of
phase 1 — the combo path works today and shares nothing with the scheme path, so
migrating it is an independent, reversible step taken after the engine is trusted.

---

## 6. Migration path

Backfill command `orders/management/commands/migrate_schemes_v2.py`:

1. Group `scheme_product` by `scheme_name` → one `Scheme` each (`code` = slugified
   name, deduped with a numeric suffix).
2. Each distinct `item_code` in the group → one `SchemeBenefit`
   (`per_qty=0, free_qty=0` — flat, quantity still supplied by the user, matching
   today's behaviour exactly).
3. Each distinct non-empty `state_code` in the group → one `SchemeAssignment`
   with `scope_type='STATE'`.
4. Each `party_product_assignments` row with `scheme_id` set → one
   `SchemeAssignment(scope_type='PARTY', scope_value=card_code, category=category)`
   plus a `SchemeTrigger(match_type='ITEM', match_value=item_code)`.
5. Existing `order_item_schemes` rows get `benefit_item_code` populated from the
   current `scheme_product.item_code` — freezing history at its present meaning.

No feature flag is needed: phase 1 is purely additive. `_extract_order_item_schemes`
accepts `scheme_v2_id` / `benefit_id` / `benefit_item_code` on an incoming line
*in addition to* the legacy `scheme_id`, and the SAP push prefers the snapshot
only when one is present. A client that sends nothing new behaves exactly as it
does today. The v1 tables are dropped only after a full order cycle runs clean.

Run it:

```
python manage.py migrate_schemes_v2 --dry-run   # report, then roll back
python manage.py migrate_schemes_v2
```

The command is idempotent and warns about any scheme that ends up with no
trigger — a legacy `scheme_product` row never attached to a party would never
fire in v2.

---

## 7. API surface

All under a `/v2/` prefix so the legacy `/orders/schemes/` endpoints feeding the
current Add Sales picker stay untouched.

| Endpoint | Purpose |
|---|---|
| `GET, POST /orders/v2/schemes/` | List (filterable by `search`, `scope_type`, `scope_value`, `include_inactive`) and create, with nested benefits/triggers/assignments |
| `GET, PATCH, DELETE /orders/v2/schemes/<id>/` | Read, update, deactivate (`?hard=true` to delete, refused if any order line references it) |
| `GET, POST, DELETE /orders/v2/schemes/<id>/assignments/` | Target a party, a state, a main group, a category, or everyone. POST takes a single object or a list, so "assign to these 40 parties" is one call |
| `POST /orders/v2/schemes/preview/` | Body: `{card_code, category, lines[]}` → the engine's proposals. Called by Add Sales on every qty change |
| `GET /orders/v2/schemes/applicable/?card_code=&category=` | Everything reaching this vendor, with the winning scope shown |

Permission classes match the surrounding scheme views (`AllowAny`) rather than
introducing a second auth story mid-migration — worth tightening across all the
scheme endpoints together, separately from this change.

The `preview` endpoint is what makes state-wide targeting usable: the salesperson
never picks a scheme from a dropdown, the engine proposes and they confirm.

Deactivation keeps the existing rule — never hard-delete a scheme referenced by an
order line (`scheme_v2` uses `PROTECT` to enforce what the current code only asks
for politely).
