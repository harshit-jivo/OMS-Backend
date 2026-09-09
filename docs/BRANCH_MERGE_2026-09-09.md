# Deploying the `test` → `production` merge (2026-09-09)

Companion to [BRANCH_MERGE_2026-08-26.md](BRANCH_MERGE_2026-08-26.md), which
recorded the merge this one partly undoes.

`test` is merged into `production` (backend) and into `live` (frontend). HAIS,
Combo Mapping and Scheme Engine v2 are **restored**, so the four revert
migrations from 26 Aug are deleted from the branch.

That last point is the whole reason this document exists.

---

## 1. Why `migrate` alone does not work

The reverts **were applied on live** on 26 Aug, so the tables and columns are
genuinely gone there. But the migrations that CREATED them are still recorded
as applied:

| Recorded as applied | Actually present on live |
| --- | --- |
| `orders/0052_orderitem_auto_free_combo` | ✗ `is_auto_free`, `combo_source_code` |
| `orders/0053_scheme_engine_v2` | ✗ four scheme tables + v2 columns |
| `orders/0054_scheme_category` | ✗ |
| `users/0026_partyproductassignment_combo_free_item` | ✗ `free_item_code`, `free_qty_per_unit` |
| `HAIS/0001_initial` | ✗ the whole `hais` schema |

Django never re-runs a migration it has recorded, so **nothing in the 77
pending migrations restores these objects**. Worse, `migrate` does not merely
skip them — it *aborts partway through*, at
`orders/0057_scheme_uom_pcs_box_only`, which does

```sql
ALTER TABLE scheme_triggers ... ;   ALTER TABLE scheme_benefits ... ;
```

against tables that are not there, leaving live half-migrated.

`scripts/restore_reverted_modules.sql` recreates exactly those objects, so the
migration run has something to alter. It is idempotent and is a no-op on the
test database (10.10.101.117), where the reverts were never applied and the
objects still hold data.

---

## 2. Before you start

**Set `SECRET_KEY` in the live `.env`.** It is currently unset, and `DEBUG=True`
is masking it: the app silently falls back to the committed
`django-insecure-…` key, which also signs every JWT
(`SIMPLE_JWT['SIGNING_KEY']` defaults to `SECRET_KEY`). The moment `DEBUG` goes
to `False`, startup fails outright — by design, see `OMS/settings.py`.

```bash
python -c "from django.core.management.utils import get_random_secret_key as k; print(k())"
```

Setting it **invalidates every token in circulation** — everyone is logged out
once. Do it inside this maintenance window rather than paying for a second one.

`HANA_SSL_CA_BUNDLE` is the only other key `.env.example` has that live lacks;
it defaults to empty and is optional. Every setting the app hard-requires is
already present.

**Take a full backup.** This is a 77-migration jump with data migrations in it.

```bash
pg_dump -h localhost -U postgres -d order_management -Fc \
        -f order_management_pre_merge_20260909.dump
```

---

## 3. The deploy

Run in this order. Steps 3 and 4 are the two that must not be swapped.

1. **Stop the app.** Data migrations run in here; concurrent writes are not safe.
2. **Pull the merged branches** — `production` (backend), `live` (frontend).
3. **Restore the reverted objects** — *before* `migrate`:
   ```bash
   psql -v ON_ERROR_STOP=1 -h localhost -U postgres -d order_management \
        -f scripts/restore_reverted_modules.sql
   ```
4. **Migrate:**
   ```bash
   python manage.py migrate
   ```
   77 migrations apply. Anything other than a clean finish → stop and restore
   the dump.
5. **Frontend:** `npm install` then `npm run build`. `test` added vitest,
   testing-library and @tanstack/react-query, so a stale `node_modules` fails
   the type-check.
6. **Start the app**, then work through §5.

---

## 4. HAIS access — no migration, and no new code

The permission module was rewritten on `test`, and this is the part that
changes how HAIS is granted.

`core/permission_registry.py` is now the single source of truth for grantable
keys, and it **already registers `HAIS`, `Scheme_Manager` and `Combo_Mapping`**.
`core.permissions.effective_keys` resolves a user's keys as:

```
extra_pages (per-user grants)  ∪  the bundle of every ACTIVE role they hold
                               ∩  the registry
```

Roles are *identity*; authority comes from keys. So HAIS access is granted by
ticking a key, not by hardcoding a role name — and `RoleCreateView`,
`RolePermissionsUpdateView` and the Role Permissions page mean the whole thing
is done in the UI, with no migration and no deploy.

Two consequences worth knowing:

* **A deactivated role's bundle grants nothing** (`effective_keys` filters on
  `role__is_active=True`). The new role must be active.
* **A new role name needs no frontend change.** `/HAIS` admits
  `permissions: ["HAIS"]`, so the key alone opens the route. The only thing tied
  to the literal name `"hais"` is the post-login shortcut in
  `pageAccess.ts:landingPathFor`; under a new name the user lands on `/Home`
  instead, which shows their permitted pages as tiles. Add a line there only if
  you want the direct jump back.

---

## 5. After the deploy: the HAIS role and Sushil

`users/0029_drop_hais` could not delete the old `hais` role because **Sushil**
holds it and `User.role` is `on_delete=PROTECT`, so it deactivated the role
instead. Sushil is a HAIS-only account — no other role, `extra_pages` empty —
and has not logged in since 26 Aug, the day HAIS was removed.

In the admin UI, in this order:

1. **Role Permissions page** → create the new role (active), and tick **Hardware
   Assets (HAIS)** in its bundle.
2. **Users page** → move Sushil onto the new role.
3. **Role Permissions page** → delete the old `hais` role. It refuses while
   anyone still holds it (`RoleDeleteView` returns *"N users still hold this
   role. Reassign them first"*), which is why step 2 comes first. The bundle
   cascades with it.

---

## 6. Verify

```bash
python manage.py check                       # no ERRORs
python manage.py migrate --plan              # "No planned migration operations"
python manage.py makemigrations --check --dry-run   # "No changes detected"
```

Note what the third one does *not* do: it compares models to migration files
and never touches the database. It is not evidence that the schema is right.
For that, check the restored objects directly:

```sql
SELECT to_regclass('public.schemes'), to_regclass('public.scheme_triggers');
SELECT count(*) FROM information_schema.schemata WHERE schema_name = 'hais';
SELECT column_name FROM information_schema.columns
 WHERE table_name = 'order_items' AND column_name IN ('is_auto_free','combo_source_code');
```

Then in the app: open Schemes, Combo Mapping and Hardware Assets, and place one
test order through Add Sales.

---

## 7. Rollback

```bash
pg_restore -h localhost -U postgres -d order_management --clean --if-exists \
           order_management_pre_merge_20260909.dump
```

then check out the previous `production` / `live` commits. There is no partial
rollback: the four deleted revert migrations no longer exist on the branch, so
`migrate <app> <earlier>` cannot walk back past them.

---

## 8. Known drift between live and the test database

Pre-existing, unrelated to this merge, and invisible to every Django check —
worth knowing before you treat 10.10.101.117 as a rehearsal for live:

| | live | .117 test |
| --- | --- | --- |
| `order_items.basic_price`, `price_list_basic` | `numeric(12,4)` | `numeric(10,2)` |
| `order_items.total` | `numeric(12,4)` | `numeric(12,2)` |
| `order_items.scheme_ltrs`, `total_ltrs` | absent | present |
| `order_items.is_scheme_visible` | `NOT NULL` | nullable |
| FK on `order_items.scheme_id` | absent | present |
| FK on `party_product_assignments.scheme_id` | absent | present |

Django's migration state matches neither exactly, and does not police column
precision, so none of this surfaces in `makemigrations --check`.
