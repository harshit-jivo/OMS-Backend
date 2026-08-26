# Branch integration into `test` — 2026-08-26

Complete record of merging **`rathod-updates`, `kamal`, `harshit` and `Mukesh`**
into `test` on `OMS-Backend`: what was done, what conflicted, how each conflict
was resolved and why, the migration-graph repair, and what is deliberately left
open.

Written so the decisions can be audited or reversed by someone who was not there.

Companion to [`OMS-Frontend/docs/BRANCH_MERGE_2026-08-26.md`](../../OMS-Frontend/docs/BRANCH_MERGE_2026-08-26.md),
which covers the same four branch names on the frontend. The two were done on the
same day but are independent repositories with independent histories. One
decision does cross over: the frontend's `Invoice_Report` page was settled by
reading `invoice/views.GetPrintReport` in this repository, which turned out to be
a strict superset of the direct-to-Crystal-service call the `Mukesh` frontend
branch used.

---

## 0. Summary

| | |
|---|---|
| Repository | `OMS-Backend` |
| Target branch | `test` |
| Starting commit | `65a1222` |
| Final commit | `4a02dcf` |
| Backup | branch `test-backup-20260826` + tag `pre-merge-20260826`, both at `65a1222` |
| Merges | 4 |
| Conflicted files resolved | 7 |
| Migration files changed | 8 (+2 new) |
| Migrations run | 2, on 2026-08-26 11:08 UTC — see §10 |
| Pushed | **no** |

Full revert: `git reset --hard pre-merge-20260826`

```
4a02dcf  fix(migrations): resolve the multi-leaf migration graph after the branch merges
18efe2d  Merge remote-tracking branch 'origin/Mukesh' into test
7762f00  Merge remote-tracking branch 'origin/harshit' into test
1ec4fc4  Merge remote-tracking branch 'origin/kamal' into test
f37f79f  Merge remote-tracking branch 'origin/rathod-updates' into test
65a1222  (pre-merge-20260826, test-backup-20260826)
```

---

## 1. Approach

Four branches were merged **one at a time**, not as a single 4-way merge, so that
any breakage is attributable to one merge rather than to the pile.

Order was chosen by ascending risk, cheapest first:

```
rathod-updates  ->  kamal  ->  harshit  ->  Mukesh
```

Safety measures taken before the first merge:

1. **Backup branch and tag** at the starting commit, so the whole exercise is one
   `git reset --hard` away from being undone.
2. **`git config rerere.enabled true`** — "reuse recorded resolution". Git
   memorises each conflict resolution and replays it if the same conflict recurs.
   This paid off immediately: the `harshit` merge had to be aborted and redone,
   and rerere meant nothing had to be resolved twice.
3. **Dry-run every merge first** with `git merge-tree --write-tree --name-only`,
   which simulates the merge without touching refs, the index or the working
   tree. Conflicts are known before committing to anything.
4. **`--no-ff` on every merge**, so each is a distinct commit that can be undone
   with `git revert -m 1` even after pushing. A fast-forward leaves nothing to
   revert.

### Divergence at the start

| branch | ahead of `test` | behind `test` | merge base | base date |
|---|---|---|---|---|
| `rathod-updates` | 1 | 0 | `65a1222` | 2026-08-24 |
| `harshit` | 3 | 41 | `ed0fd30` | 2026-08-11 |
| `kamal` | 25 | 31 | `423f7dd` | 2026-08-21 |
| `Mukesh` | 19 | **62** | `9f0f8ee` | **2026-07-25** |

`Mukesh` had been diverged for a month. That is the root cause of most of what
follows — it forked before several features existed, so "absent on Mukesh" and
"deliberately removed on Mukesh" look identical to git and had to be told apart
by hand.

> **Not merged:** `origin/production` is 35 ahead / 31 behind `test`. It was out of
> scope for this exercise but `test` remains substantially diverged from it.

---

## 2. Conflicts, and how each was resolved

### 2.1 Prediction vs reality

| branch | predicted by dry-run | actually conflicted |
|---|---|---|
| `rathod-updates` | clean | clean |
| `kamal` | 2 files | 2 files |
| `harshit` | 3 files | **0** — see §3 |
| `Mukesh` | 7 files | 5 files |

### 2.2 `rathod-updates` — clean

One commit, `approvals/services.py`, +14/−1. Nothing to decide.

### 2.3 `kamal` — 2 conflicts

**`hana/services/connection.py` — `Queries.get_costing_code`**

Conflict was in the docstring only. Both sides were kept: kamal's explanation
(PrcCode is max 8 chars, e.g. `SUNFLOWER` → `SUNFLOWR`; `PrcName` is not unique
across dimensions) plus `test`'s note that the lookup is schema-scoped
(OIL / BEVERAGE / MART).

**This conflict also hid a silent code loss — see §4.1.**

**`orders/urls.py`**

Kamal added a third `from .views import ...` line. Inspection showed its symbol
list is exactly the union of the two `from .views import` lines already present
above it, so it was **dropped as redundant** rather than merged. No import lost.

> Pre-existing sloppiness noted, not fixed: `orders/urls.py` imports from
> `.views` on two separate lines with heavily overlapping symbol lists.

### 2.4 `harshit` — 0 conflicts (after diagnosis)

The first attempt produced three *whole-file* conflicts. That turned out to be a
line-ending artefact, not content. Full diagnosis in §3. The merge was aborted
and redone with `-Xrenormalize`, which merged all three cleanly.

### 2.5 `Mukesh` — 5 conflicts

| file | resolution | rationale |
|---|---|---|
| `einvoice/sap.py` | **Mukesh's side entirely** | Strict superset: the whole buyer-GSTIN block (`_REGISTERED_GST_TYPES`, `_addr_key`, `_bp_addresses`, `_master_gstin`, `normalize_buyer_gstin`) plus the `fetch_invoice_for_irn` that calls it. `test`'s side was only an older docstring. |
| `einvoice/mapping.py` | **Mukesh's paragraph kept, repositioned** | Mukesh's docstring text had landed in the middle of the field-source list. Moved below it. No text dropped. |
| `serviceLayer/service.py` | **`test`'s side** | Mukesh had `cls._schema_for(branch)`. That method does not exist — the class defines `schema_for` (line 12), and `schema_for` is what lines 53, 125 and `serviceLayer/views.py:62` all call. Mukesh's line would raise `AttributeError` on every call. A rename artefact; behaviour preserved under the name the merged tree uses. |
| `OMS/settings.py` | **`test`'s side** | Both sides define the same two names. `test`'s version reads *every* environment variable Mukesh's does (`HANA_BEVERAGE_COMPANY_DB`, `HANA_MART_COMPANY_DB`) **plus** fallbacks (`HANA_DB_BEVERAGE_NAME`, `HANA_COMPANY_DB_BEVERAGES`, `HANA_DB_MART_NAME`) and adds `HANA_MART_COSTING_CODE`. Mukesh's bare `config('HANA_BEVERAGE_COMPANY_DB')` crashes Django on boot if that variable is commented out in `.env`, which it has been. No setting lost. |
| `docs/ap-invoice-service-layer.md` | **Mukesh's side** (add/add) | Purely additive: Mukesh's version adds a whole `## 5. OMS implementation` section documenting the AP endpoints its branch introduces, and renumbers the following sections. |

---

## 3. The line-ending trap

Worth its own section, because the symptom is deeply misleading.

### Symptom

Merging `harshit` produced conflicts spanning entire files:

```
orders/models.py:1        <<<<<<< HEAD
orders/models.py:785      =======
orders/models.py:1576     >>>>>>> origin/harshit
```

783 (ours) + 790 (theirs) + 3 markers = 1576. Every line of the file, on both
sides, inside one conflict block. Same for `orders/views.py` and
`orders/scheme_engine.py`.

### Diagnosis

Comparing the three merge stages as raw blobs:

| | insertions | deletions |
|---|---|---|
| base → ours | 6 | 2 |
| base → **theirs** | **790** | **779** |

Theirs rewrote every line. Two checks identified why:

```bash
git diff --ignore-cr-at-eol --stat <ours_blob> <theirs_blob>
#  -> 19 insertions, 12 deletions      <- the REAL change

git grep -c -P '\r$' <theirs_blob>
#  -> 790                              <- every one of 790 lines ends with CR
```

`harshit` had committed those files with **CRLF baked into the blob**, while
`test`'s blobs are LF. Git could not align the two sides, so it fell back to
"the whole file conflicts" — over a genuine 31-line difference.

> **False lead:** `git ls-files --eol` reported `i/lf` for all three stages,
> which appears to rule out line endings. It does not — that reflects the
> normalised index view. Likewise, exporting blobs with PowerShell `>` rewrites
> the endings and destroys the evidence. Only `git diff --ignore-cr-at-eol` and
> `git grep -P '\r$'` on the blob SHAs are trustworthy here.

### Fix

```bash
git merge --abort
git merge --no-ff -Xrenormalize origin/harshit
```

`-Xrenormalize` runs a virtual check-out/check-in of all three stages to
normalise line endings before merging. Result: **clean merge, zero conflicts**,
15 files, +685/−33. `-Xrenormalize` was used for the `Mukesh` merge too.

### Prevention

`.gitattributes` added at the repository root:

```gitattributes
* text=auto
*.py *.md *.sql *.json *.txt *.sh    text eol=lf
*.bat *.cmd                          text eol=crlf
*.png *.jpg *.pdf *.woff2 ...        binary
```

**Deliberately not done:** `git add --renormalize .`. That would rewrite line
endings across hundreds of files in one commit and conflict with every open
branch. Do it as its own commit when nobody has work in flight.

---

## 4. Two problems the merge caused, and one it inherited

Both of the first two were found by verification, not by git — neither produced
a conflict marker.

### 4.1 kamal's SQL fix was being silently dropped

The `hana/services/connection.py` conflict was **docstring-only**, but the merged
function body came out *without* the two lines kamal's commit added:

```sql
SELECT TOP 1 T0."PrcCode"
FROM "{s}"."OPRC" AS T0
WHERE T0."PrcName" = '{safe_prc_name}'
  AND T0."DimCode" = 1        <-- missing after the merge
  AND T0."Active" = 'Y'       <-- missing after the merge
```

Verified against all three stages: base and ours are unscoped, kamal's adds the
scoping. Without it, `PrcName` is not unique across dimensions — `KARNATAKA`
exists twice under `DimCode 5`, one row inactive — so an unscoped `TOP 1` can
return another dimension's code, **which SAP accepts and books to the wrong
profit center**.

Restored by hand during resolution.

> **Lesson:** when a conflict is resolved, diff the result against *both* parents,
> not just the conflict block. Git's hunk boundaries do not always contain the
> whole of what each side changed.

### 4.2 The merge broke `InvocieHistory` writes

After merging, the tree contained:

- `invoice/views.py` — calls `describe_request_device(request)` at **4 sites**,
  three of them spread into `InvocieHistory(...)` as `**describe_request_device(request)`
- `invoice/models.py` — **no** `device_id` / `device_name` fields

Every invoice status change would have raised:

```
TypeError: InvocieHistory() got unexpected keyword arguments:
'device_id', 'device_name'
```

How it happened:

| branch | `models.py` device fields | `views.py` calls | has `0019_..._device_*` migration |
|---|---|---|---|
| `test` (pre-merge) | yes | 4 | yes |
| `kamal` | no | 4 | yes |
| `harshit` | yes | 4 | yes |
| `Mukesh` | no | 0 | **no** |

`kamal` removed the fields; `Mukesh` never had the feature at all (it forked
before `0019` landed). Two sides said "absent", so the merge took absent — while
the `views.py` that writes them survived from the other two.

**Resolution:** the fields were restored to `invoice/models.py`. This follows
harshit's `0024_restore_invocie_history_device_columns` (2026-08-21), which is
nine days newer than kamal's removal (2026-08-12) and explicitly states that
removing them was the wrong direction, for exactly this reason.

### 4.3 Inherited: a model field with no migration

`PartyProductAssignment.parent_item_code` was added to `users/models.py` by
harshit's `de95c81` **without a migration** — its two siblings on the same
commit, `free_item_code` and `free_qty_per_unit`, both got one.

Pre-existing, not caused by the merge: `origin/harshit` alone already fails
`makemigrations --check`. Fixed here because it blocks the app either way.

---

## 5. Lost-symbol audit

Rather than trusting a file-by-file reading, every merged branch was compared
against the finished tree at AST level: for each tracked `.py` file, every
module-level function, class, method and `UPPER_CASE` constant on the branch was
checked for presence in the result.

| branch | files checked | missing |
|---|---|---|
| `origin/Mukesh` | 173 | **0** |
| `origin/harshit` | 178 | **0** |
| `origin/kamal` | 187 | 1 |
| `origin/rathod-updates` | 267 | 1 |
| `test` (pre-merge) | 267 | 1 |

The single hit is the same symbol in all three cases:

```
users/views.py -> method:ComboMappingsView._free_product_payload
```

**Deliberate, not a merge accident.** Harshit's `de95c81` removed the definition
*and* its only call site together (2 occurrences → 0), and `Mukesh` dropped it
independently. No orphaned caller remains in the merged tree.

The audit script is not committed; it is reproducible from this description in a
few minutes and is not part of the build.

---

## 6. The migration graph

This was the largest piece of work, and it affected **four** apps, not the two
predicted before merging.

### 6.1 What was broken

Django refuses to run when an app has more than one leaf node:

```
Conflicting migrations detected; multiple leaf nodes in the migration graph
```

| app | leaves after merging | |
|---|---|---|
| `invoice` | 2 | `0021_invoicelog_delete_reason_...` (kamal), `0024_restore_invocie_history_device_columns` (harshit) |
| `orders` | 3 | `0053_alter_order_order_type` (kamal), `0058_alter_order_order_type`, `0058_scheme_benefit_uom_snapshot` |
| `uilabels` | 2 | `0003_uilabel_column_defaults` (harshit), `0004_seed_po_number` |
| `users` | 1 | leaf OK, but a model field had no migration (§4.3) |

Plus two pairs making **the same schema change twice**, which raises
`Field 'x' already exists` while Django builds project state:

| change | on `test` | on `kamal` |
|---|---|---|
| `Order.warehouse_code` | `0056_order_warehouse_code` | `0052_order_warehouse_code` |
| `Order.order_type` choices | `0058_alter_order_order_type` | `0053_alter_order_order_type` |

### 6.2 Why the duplicates existed

Both were **deliberate re-cuts**, documented in their own comments.

`orders/0052_order_warehouse_code` said:

> "There, this column arrived as 0056, chained through
> `0052_orderitem_auto_free_combo` .. `0055_fix_combo_source_code_nullable`. None
> of those are wanted on production yet, so the column is re-cut as a direct
> child of 0051 instead."

That reasoning was correct at the time and is now void: `test` carries the
scheme-engine chain regardless, because the same merge brought it in.

A second pattern appears in `invoice/0020_invoice_log_is_deleted_default`,
`invoice/0022_invocie_history_drop_device_columns` and
`uilabels/0003_uilabel_column_defaults`. All three were written **blind**, as
repairs against a database where another branch's migrations were *recorded as
applied but whose files were missing from the tree*. Their docstrings say so:

> "The column was added by a migration whose file is no longer in the tree
> (`0021_invoicelog_delete_reason_...`, still recorded as applied), so the model
> and the schema have diverged."

The merge brought those files back. The workarounds are still safe — all are
guarded — but their dependencies pointed at the wrong parents.

### 6.3 Changes made

| file | change |
|---|---|
| `invoice/0020_remove_invociehistory_device_id_and_more.py` | operations **emptied** — superseded by `0024` |
| `invoice/0020_invoice_log_is_deleted_default.py` | re-parented `0019` → **`0021`** (the migration that creates `is_deleted`) |
| `invoice/models.py` | `device_id` / `device_name` restored (§4.2) |
| `orders/0052_order_warehouse_code.py` | operations **emptied** — duplicate of `0056` |
| `orders/0053_alter_order_order_type.py` | operations **emptied** — duplicate of `0058` |
| `orders/0056_order_warehouse_code.py` | rewritten as `SeparateDatabaseAndState` with `ADD COLUMN IF NOT EXISTS` |
| `orders/0057_merge_20260822_1319.py` | dependency on `0053_alter_order_order_type` added, folding kamal's lineage in |
| `orders/0059_merge_20260826_branches.py` | **new** — joins the two remaining leaves |
| `uilabels/0003_uilabel_column_defaults.py` | re-parented `0002` → **`0004_seed_po_number`** |
| `users/0030_partyproductassignment_parent_item_code.py` | **new** — the migration `de95c81` omitted |

### 6.4 Resulting chains

**`invoice`** — now fully linear:

```
0019_invociehistory_device_id_invociehistory_device_name   (adds device fields)
  -> 0020_remove_invociehistory_device_id_and_more         (NO-OP)
    -> 0021_invoicelog_delete_reason_...                   (adds is_deleted etc)
      -> 0020_invoice_log_is_deleted_default               (RunSQL default on is_deleted)
        -> 0022_invocie_history_drop_device_columns        (state remove + DROP IF EXISTS)
          -> 0023_recreate_invoice_ref_logs
            -> 0024_restore_invocie_history_device_columns (state add + ADD IF NOT EXISTS)  <- LEAF
```

End state: device fields **present**, delete fields **present** — matching
`models.py` and the live `views.py`.

**`orders`**:

```
0051_webpushsubscription
 |- 0052_order_items_orphan_column_defaults ------------------------.
 |- 0052_order_warehouse_code (NO-OP)                               |
 |    `- 0053_alter_order_order_type (NO-OP) --------------------.  |
 `- 0052_orderitem_auto_free_combo                               |  |
      `- 0053_scheme_engine_v2 -> 0054 -> 0055 -> 0056 --------.  |  |
                                                   |           |  |  |
                                            0057_merge_20260822_1319 (3 deps)
                                                   |           `- 0058_alter_order_order_type -.
                                                   `- 0057_scheme_uom_pcs_box_only             |
                                                        `- 0058_scheme_benefit_uom_snapshot ---|
                                                                                               |
                                                          0059_merge_20260826_branches  <- LEAF
```

### 6.5 Why superseded migrations were emptied, not deleted

`django_migrations` already records them as applied on the shared databases.
Deleting the files would strand those rows and make Django complain about
migrations it cannot find.

Emptying is safe in both directions because **every database half is guarded**:

- a database that **did** run `0052_order_warehouse_code` already has the column;
  `0056`'s `ADD COLUMN IF NOT EXISTS` is a no-op there
- a database that **did not** gets the column from `0056`
- likewise `invoice`: `0022` drops with `IF EXISTS`, `0024` adds with
  `IF NOT EXISTS`

Both converge on the same schema.

### 6.6 Verification

Validated with Django's own `MigrationLoader`, `connection=None` — graph
construction and state replay only, **no database connection, no `migrate`**:

```
1. building graph .................. 248 migrations loaded
2. conflict detection .............. OK - exactly one leaf per app
3. replaying every operation ....... OK - all operations applied cleanly to state
4. migrations vs models.py ......... OK - no pending changes
5. device fields on InvocieHistory .. migration state: yes / models.py: yes
6. warehouse_code added ............ exactly once
```

Step 3 is the one that matters most: it proves there is no duplicate `AddField`
and no `RemoveField` against a field that is already gone. Step 4 is the
equivalent of `makemigrations --check`.

Leaf count, every app with migrations:

```
approvals 1 · attachments 1 · audit 1 · core 1 · devices 1 · einvoice 1
HAIS 1 · invoice 1 · legal 1 · notifications 1 · orders 1 · payments 1
sap_sync 1 · SKU 1 · tracker 1 · uilabels 1 · users 1
```

---

## 7. Preserved for later

Kamal's `0053_alter_order_order_type` made the `order_type` `AlterField`
**state-only** on purpose, and that reasoning is worth keeping even though the
migration is now a no-op:

> `choices` is validated in Python and never reaches Postgres — there is no CHECK
> constraint behind it — and the column is unchanged (`varchar(20)`, default
> `'PARTY'`). A bare `AlterField` still hands the schema editor an
> `ALTER COLUMN TYPE` that rewrites nothing but takes an **ACCESS EXCLUSIVE lock**
> on `orders`.

`0058_alter_order_order_type` does **not** do this. If that lock is a problem
when `0058` reaches a live database, split `0058` the same way rather than
reviving the emptied migration. The note is retained in the emptied file.

---

## 8. Open items

Nothing below was done, deliberately.

| item | why it was left |
|---|---|
| ~~**`migrate` has not been run**~~ | **Done 2026-08-26 11:08 UTC — see §10.** Only 2 migrations were ever pending, and neither altered the schema. The `invoice` device-column churn was never a risk here: 0020–0024 were already applied to the shared database by their authors. |
| **Nothing pushed** | `test` is a shared branch and this is 4 merges plus a migration rewrite. Tell the team first. |
| **`git add --renormalize .`** | Would rewrite line endings across hundreds of files and conflict with every open branch. Own commit, quiet moment. |
| **`origin/production` not merged** | 35 ahead / 31 behind `test`. Out of scope here; the divergence remains. |
| **`0058_alter_order_order_type` lock** | See §7 — a judgement call for whoever applies it to production. |
| **Device-column churn** | Whether `invoice/0022` → `0024` should be collapsed into a single no-op pair before this reaches production. |

---

## 9. Reproducing the checks

```bash
# predict a merge without touching anything
git merge-tree --write-tree --name-only test origin/<branch>

# is a whole-file conflict really a line-ending problem?
git ls-files -s -- <path>                       # get the stage blob SHAs
git diff --ignore-cr-at-eol --stat <ours> <theirs>
git grep -c -P '\r$' <theirs>                   # == line count means CRLF blob

# migration graph, no database
python - <<'PY'
import django; django.setup()
from django.db.migrations.loader import MigrationLoader
loader = MigrationLoader(None, ignore_no_migrations=True)
print(loader.detect_conflicts() or "one leaf per app")
loader.project_state()          # raises if any operation cannot apply
PY

# undo everything
git reset --hard pre-merge-20260826
```

---

## 10. Applying the migrations — 2026-08-26 11:08 UTC

Run against `order_management` on `138.252.101.117` (PostgreSQL 16.15), after a
`pg_dump` was taken and verified.

### 10.1 Only two migrations were ever pending

The merge added 19 migration files, but `migrate --plan` reported just two:

```
orders.0059_merge_20260826_branches               <- operations = [], bookkeeping
users.0030_partyproductassignment_parent_item_code
```

Everything else — `invoice` 0020–0024, `orders` 0052–0058, `tracker` 0015–0020,
`uilabels` 0003 — was **already applied** to the shared database by the branch
authors. The merge brought the *files* into `test`; the *schema* had been there
for weeks.

That retires the largest worry in §8. The `invoice` device-column churn
(0022 drops, 0024 re-adds) was never going to run: both were long since applied,
so there was no drop-then-restore cycle to survive.

### 10.2 `users.0030` would have failed on first contact

It was written during the merge as a plain `AddField`. It would have died:

```
ProgrammingError: column "parent_item_code" of relation
"party_product_assignments" already exists
```

`parent_item_code` was **already on the database** — `varchar(50)` NULL, with
both `party_product_assignments_parent_item_code_2eeb8c2d` and its `_like`
counterpart — while `django_migrations` held no row for the migration.

The cause is the same defect §4.3 already records, one step further along.
harshit's `de95c81` added the field to `users/models.py` without committing its
migration. What §4.3 did not know is that he had also **applied** that
uncommitted migration to the shared database. So the schema ran ahead of the
tree, and the migration written to close the gap collided with his column.

Fixed in `f8f99a9` by splitting it into `SeparateDatabaseAndState` with guarded
DDL — the pattern `orders/0056` already uses for exactly this reason. The SQL
reproduces what `AddField(db_index=True)` emits on PostgreSQL (column, btree
index, and the `varchar_pattern_ops` index backing `LIKE`), with Django's own
deterministic index names hardcoded so a database built from scratch ends up
identical to the one that already has them.

```sql
ALTER TABLE party_product_assignments
  ADD COLUMN IF NOT EXISTS parent_item_code varchar(50) NULL;
CREATE INDEX IF NOT EXISTS party_product_assignments_parent_item_code_2eeb8c2d
  ON party_product_assignments (parent_item_code);
CREATE INDEX IF NOT EXISTS party_product_assignments_parent_item_code_2eeb8c2d_like
  ON party_product_assignments (parent_item_code varchar_pattern_ops);
```

Every statement is guarded, so the migration is a no-op where the column exists
and correct where it does not.

> **How it was caught.** By the `pg_dump` verification step, not by any migration
> check. `pg_restore -l` on the backup listed indexes on `parent_item_code` — in
> a dump taken *before* the migration ran. Graph validation had passed cleanly
> at every stage, because `MigrationLoader(None)` never opens a database
> connection and therefore cannot see a schema that has drifted ahead of the
> tree. **A migration graph that validates is not a migration that will run.**

### 10.3 Result

```
orders.0059_merge_20260826_branches                 applied 11:08:05.283 UTC
users.0030_partyproductassignment_parent_item_code   applied 11:08:05.420 UTC
```

Neither changed the schema; both simply brought `django_migrations` into line
with what the database already had.

Verified afterwards:

| check | result |
|---|---|
| `migrate --plan` | `No planned migration operations.` |
| `parent_item_code` | `varchar(50)`, nullable — intact |
| indexes | both present — intact |
| `party_product_assignments` | 13,423 rows, 24 with a non-null `parent_item_code` |
| `makemigrations --check --dry-run` | `No changes detected`, exit 0 |

### 10.4 Reversing, and why `--fake` is now the right tool

§8's revert advice no longer applies as written. Reversing `users.0030` would
drop `parent_item_code` — and that column is **not** the migration's to destroy.
It predates the migration and holds 24 live combo-parent mappings written by
harshit's work.

```bash
# unwinds the bookkeeping only, leaves the column and its data alone
python manage.py migrate users 0029_merge_combo_free_item_roles --fake
```

Leave `orders.0059` applied either way: it has `operations = []`, so unapplying
it achieves nothing while forcing a target of one of its two parents, which
would take a real `0058` branch down with it.

The backup remains the safer instrument:
`backups/order_management_pre-merge-20260826.dump`, custom format, all three
schemas (`public`, `payments`, `hais`), 118 tables with data.
