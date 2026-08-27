# `orders/views.py` — deep read

**5,667 lines · 54 view classes · 3 uses of `transaction.atomic` or
`select_for_update`.**

This file was singled out because it is the largest module in the codebase, it
holds the order lifecycle, and two *other* modules (`core/models.py` and
`approvals/services.py`) name defects inside it as the reason they exist. This
is the record of reading it.

Everything below is cited by line number against the file as of 2026-08-26.

---

## 1. Why this file is the refactor's centre of gravity

`approvals/services.py` and `core/models.py` were both written as deliberate
corrections to code in here. Their docstrings say so. That means the newer,
correct implementations already exist — **the work is migration onto them, not
invention.**

| written to fix | the code it replaces |
|---|---|
| `approvals/services.py` (751 LOC, atomic + locked) | `UpdateOrderStatusView.post` — the ~380-line approval flow |
| `core.next_document_number()` (locked, gapless) | the order-number generator |

---

## 2. Confirmed defects

Each of these was read in full, not inferred.

### 2.1 🔴 Order-number generation race — `views.py:2878-2893`

```python
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
...
order = Order.objects.create(order_number=order_number, ...)
```

Read-then-write with **no lock and no transaction**, and `order_number` is
unique. Two concurrent creates read the same "last" row, generate the same
number, and the second `create()` raises `IntegrityError` — a 500 to a user who
did nothing wrong.

`core.next_document_number()` already solves exactly this, under
`select_for_update()`, and its docstring cites this very code as what it
replaces. **Fix = call it.**

> Also note the ordering is a *string* sort (`-order_number`). It works only
> because the counter is zero-padded to 4 digits. It silently breaks at 10,000
> orders in one day, or if any row is ever written with different padding.

### 2.2 🔴 Approval race — `UpdateOrderStatusView.post`, `views.py:3346`

No `@transaction.atomic`. No `select_for_update()`. ~380 lines with many
dependent writes. Two approvers acting simultaneously can both pass the pending
check and double-advance the document — exactly as `approvals/services.py`
states.

Worse than the summary suggests: **the order is saved before it is validated.**

```python
order.status = status_obj
...
order.save()                                  # line 3407  <- committed here

...                                           # ~60 lines later
if is_rate_approved or is_rate_rejected:
    rate_approval = _get_order_rate_approval(order, user)
    if not rate_approval:
        order.status = previous_status
        order.save(update_fields=["status"])  # line 3467  <- manual undo
        return Response(..., status=403)
```

The rollback is a hand-written compensating write. Between line 3407 and line
3468 the order is in the wrong state and any concurrent reader sees it. If the
process dies in that window, it stays there permanently. A transaction would
make the whole thing atomic for free.

### 2.3 🔴 Any authenticated user can transition any order — `views.py:3347`

`permission_classes = [IsAuthenticated]` — that is the *entire* authorization
check on the endpoint that drives the order lifecycle.

Role checking happens ad-hoc inside the method, and only on some paths: the
rate-approval branch calls `_get_order_rate_approval(order, user)`, but the
billing and auditor transitions have no equivalent. So an authenticated user
with no relevant role can move an order from Billing to Auditor to Completed.

### 2.4 🟠 `UpdateOrderView.put` destroys items with no transaction — `views.py:2614`

```python
order.items.all().delete()
...
for item in items:
    created_items.append(_create_order_item(order, item, _to_float, _to_bool))
```

Delete-then-recreate, unwrapped. Any failure between the two — a bad payload, a
DB hiccup, a scheme lookup raising — leaves the order with **zero items**, and
the originals are gone. Not recoverable from the request.

### 2.5 🟠 `_get_rate_approval_reason` is defined twice — `views.py:358` and `views.py:377`

Two functions, same name, ~20 lines apart, **different logic**. Python keeps the
second; the first is dead code that still reads as live.

They do not agree. The first flags only `basic_price < price_list_basic`. The
second flags four conditions including both-zero and `price_list_basic == 0`,
and additionally exempts combo free halves. Anyone reading the first one — which
appears first — draws the wrong conclusion about when an order needs rate
approval.

### 2.6 🟠 `@lru_cache` over database rows — `views.py:843`

```python
@lru_cache(maxsize=1)
def _get_state_display_map():
    ...State.objects.filter(is_active=True)...
```

Cached for the **lifetime of the process**, with no invalidation. Add a state,
rename one, or deactivate one, and the running workers keep serving the old map
until restarted. Different workers can disagree with each other.

### 2.7 🟡 `ai_order_summary` — `views.py:84`

```python
@csrf_exempt
def ai_order_summary(request):
    data = json.loads(request.body)
    result = get_order_summary(data)
    return JsonResponse({"summary": result})
```

A raw Django view, not DRF: no authentication, no permission class, CSRF
disabled, no serializer, no error handling. `json.loads` on a malformed body is
an unhandled 500. Whatever it forwards to the AI service is caller-controlled.

---

## 3. The status-name coupling — the biggest structural problem

**71 places in this file key business logic off status *name* substrings.**

```python
if "billing" in prev_name or "auditor" in prev_name: ...
def _is_rejection_status(status_obj):
    text = f"{status_obj.code} {status_obj.name}".lower()
    return 'reject' in text
```

`OrderStatus.name` is display text. Renaming a status to "Billing Review" or
"Rejected by Auditor" silently reroutes the order flow — no error, no test
failure, just different behaviour.

The codebase already knows this is wrong. `tracker` solves the same problem
correctly and documents the rule:

> Stage config is data, not code. Names, order, thresholds and status choices
> are admin-editable. Logic keys on `code` — **renaming a stage is safe,
> changing its code is not.**

`tracker/services.py` keys off `Stage.code`. `orders/views.py` keys off
`OrderStatus.name`, 71 times.

### 3.1 Hardcoded primary keys, and an ID conflation

Where it does not match on names, it matches on literal IDs:

```python
OrderStatus.objects.filter(id=3).first()     # 3380  "Billing Approval"
status_obj.id == 3                           # 3412
status_obj.id == 6                           # 3452  "approved"
MART_STATUS_PENDING_ID = 12                  # 683
MART_STATUS_APPROVED_ID = 6
MART_STATUS_REJECTED_ID = 7
MART_STATUS_COMPLETED_ID = 9
```

With a comment admitting the fragility:

> `OrderStatus` rows already present in the DB (managed there, not via
> migration)

**No migration guarantees these primary keys.** A fresh environment built from
migrations gets whatever IDs the inserts happen to produce, and the Mart flow
silently targets the wrong statuses.

There is also a genuine conflation. `APPROVER_REJECTED_ACTION_ID = 7` is defined
as an **action** ID at line 66, then compared against a **status** ID:

```python
is_rate_rejected = (
    prev_name == "rate approval"
    and (status_obj.id == APPROVER_REJECTED_ACTION_ID or ...)
)
```

It works only because status 7 and action 7 both happen to mean "rejected".
That is a coincidence the code depends on.

---

## 4. Authorization: 24 of 55 routes are open

Per [`API_SURFACE.md`](API_SURFACE.md), 24 of the 55 `orders` routes require no
authentication. Two patterns produce it.

**Commented-out permissions:**

```python
class GetOrdersByItemView(APIView):
    # permission_classes = [IsAuthenticate]      # 5412 — note the typo, too
```

Commented out, so it inherits the DRF default of `AllowAny`. The identifier is
also misspelled (`IsAuthenticate`), so restoring the line would raise
`NameError` — evidence it was never working.

**Deliberate propagation.** The scheme v2 views chose it on purpose, at
`views.py:5427`:

> Permission classes match the existing scheme views (`AllowAny`) so this lands
> behind the same gate as `SchemeManageListView` / `SchemeDetailView` rather
> than introducing a second, inconsistent auth story mid-migration.

The reasoning is sound in isolation — consistency during a migration — but the
result is that **discount and pricing scheme management is fully public**:
`SchemeV2ListCreateView`, `SchemeV2DetailView`, `SchemeAssignmentView`,
`CreateSchemeView`, `SchemeDetailView`. Anyone can create a scheme that gives
away free stock.

This is the strongest argument for Phase 2.1 (`DEFAULT_PERMISSION_CLASSES =
IsAuthenticated`): it converts "I matched the neighbouring insecure default"
into "I must state the exception explicitly."

---

## 5. What is good in here

Not everything. The parts that were extracted are well built, and the refactor
should preserve them exactly:

- **`scheme_engine` integration** (`_apply_engine_schemes`, `views.py:561`).
  Re-resolves giveaways server-side because *"the client sends back the
  proposals it displayed, but it is not the authority on them."* It fills only
  gaps, never overwriting a hand-typed override. This is the correct trust
  boundary and it is documented.
- **`benefit_item_code` is a snapshot.** The SAP push ships exactly what was
  stored, so editing a scheme later cannot change what an approved order sends.
- **`_scheme_v2_category_allows`** mirrors the engine's category wall at save
  time, so a stale or hand-rolled client cannot persist a scheme the UI would
  never have offered.
- **Scheme delete is a deactivate** — `OrderItemScheme.scheme_v2` is `PROTECT`,
  so a referenced scheme cannot be deleted; the giveaway record must survive.
- **`_get_next_order_flow_status`** — the order lifecycle is genuinely
  configurable per party, per category, per flow type (`ASM` vs `BILLING`), via
  `OrderFlowConfig` / `PartyOrderFlowConfig`. This is real, load-bearing
  behaviour and must not be flattened into a fixed pipeline during a refactor.

---

## 6. Refactor sequence for this file

Ordered so each step is independently shippable and testable.

| # | change | depends on |
|---|---|---|
| 1 | Characterisation tests for the order lifecycle: create, each transition, reject, FOC, Mart | — (Phase 0) |
| 2 | Order numbering → `core.next_document_number()` | 1 |
| 3 | Wrap `UpdateOrderView.put` and `CreateOrderView.post` in `transaction.atomic` | 1 |
| 4 | Delete the dead `_get_rate_approval_reason` (`:358`) | 1 |
| 5 | Replace `@lru_cache` with a cache that can be invalidated | — |
| 6 | Give `OrderStatus` a stable `code` and migrate all 71 name checks onto it | 1 |
| 7 | Replace hardcoded status IDs with lookups by `code` | 6 |
| 8 | Move `UpdateOrderStatusView` onto `approvals/services.py` | 1, 6, 7 |
| 9 | Split the file by domain: orders / schemes / parties / products / dashboard / mart | 1–8 |
| 10 | Real permission classes per view | Phase 2 |

**Step 6 is the keystone.** Steps 7, 8 and 9 are all cheap once status identity
is stable, and all dangerous before it — because until then, any refactor that
touches a status name changes behaviour invisibly.

Step 1 is not optional. `orders` has 6 tests in `tests.py` covering the whole
app; the scheme engine's 45 are the only real coverage, and they test the pure
function, not this file.

---

## 7. Coverage note

Read in full: lines 1–960, 2450–2650, 2870–2910, 3346–3520, 5405–5525, plus
targeted reads of every site named above. The remainder — chiefly the dashboard
aggregations (`WDashboardKPIView`, `DashboardChartsView`, `~1275–2240`), the
Mart flow (`~3985–4230`) and the quotation views (`~5178–5410`) — was surveyed
structurally rather than line by line.

Nothing in the unread regions changes the findings above, but they may hold
further defects of the same kinds: unguarded multi-write sequences, name-keyed
status logic, and missing permission classes. The three patterns are systemic
in this file, not localised.
