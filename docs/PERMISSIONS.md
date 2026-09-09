# PERMISSIONS — Key-Based Authorization & the Role Permissions Matrix

Developer and operator reference for the permission system introduced in the
2026-09 access-control work (Phases 1–5). Covers the architecture, the API,
the deploy/migration order, the transitional fallbacks and their cleanup
contract, and how to extend the system when adding a module.

Companion reading: `users-module-workflow.md`, `core/permissions.py` (heavily
documented in-source), `core/permission_registry.py`.

---

## 1. Why this exists

Before this work, authority was decided by **three mechanisms that could not
see each other**:

| Mechanism | Where | Problem |
|---|---|---|
| `extra_pages` per-user grants | `User.extra_pages` JSON | No role-level grants; ticked per user, forever |
| Hardcoded `role_name == '...'` | ~31 sites, mostly `orders/` | Changing what a role can do = a deploy; most read the primary `role` FK alone, ignoring `extra_roles` |
| `tracker/permissions.py:ROLE_PAGE_MAP` | tracker only | A third vocabulary; invisible to the other two |

Concrete failures this produced:

* **Every order write endpoint was `IsAuthenticated` only.** An auditor — or
  a legal/HAIS/tracker/payments account — could create a sales order by
  calling `POST /orders/create/` directly. The frontend route table was the
  only gate, and hiding a link is not access control.
* **No screen showed what a role could do**, and nothing but a deploy could
  change it.
* A user granted a role via `extra_roles` was invisible to half the checks.

## 2. The model

**Role = identity. Keys = authority.** (The rule `payments/permissions.py`
argued for, generalized project-wide.)

```
core/permission_registry.py        REGISTRY: every grantable key, in code
        │
        │  seeds (migrations 0032, 0033) · matrix page edits
        ▼
users.RolePermissions              one row per role: {role → [keys]}
users.User.extra_pages             per-user grants (unchanged, same strings)
        │
        ▼
core.permissions.effective_keys(user)
    = union(active roles' bundles, extra_pages) ∩ REGISTRY
    admin ⇒ every registered key, implicitly
        │
        ├─ backend:  HasKey('orders.sales.create') in get_permissions()
        └─ frontend: `permissions` field on login/profile → session.grants
                     → can(key) → routeAccess / Guard / sidebar
```

Properties, all enforced by code or test:

* **One registry.** A key not in `core/permission_registry.py` cannot be
  granted (PUT rejects it by name), cannot be checked (`HasKey` raises at
  construction — a typo in a view crashes on boot, not silently), and if
  stored anyway is **inert** (`effective_keys` intersects with the registry).
* **Admin holds everything.** `is_admin()` (the `admin` role held as primary
  or extra, or `is_superuser`/`is_staff`) expands to all registered keys. No
  admin column on the matrix, no admin bundle to maintain.
* **Deactivated roles grant nothing.** `effective_keys` skips
  `is_active=False` roles' bundles — "Deactivate" on the matrix page is a
  real off-switch that keeps the bundle for later reactivation.
* **Graceful degradation.** On a database where migrations 0031+ have not
  run, `effective_keys` falls back to `extra_pages ∩ registry` — exactly the
  pre-existing behaviour — instead of erroring. This is why the bundle lives
  in its own table (`users_role_permissions`) rather than a new column on
  `users_role`: a missing table only fails when touched; a missing column
  breaks every `user.role` fetch, login included.

### Key naming

Two generations coexist **deliberately**:

* New keys: `module.resource.action` — `orders.sales.create`,
  `invoices.review.decide`.
* Legacy keys: the flat strings web + mobile already exchange —
  `App_User`, `Payments_Create`, `Tracker_Queue`. Registered verbatim;
  renaming them would break the mobile contract for zero gain.

Keys are **opaque identifiers**. Nothing parses them back into
app/action/model — the EXIM project derives structure by splitting permission
strings on underscores, which mangles every custom name (`sync_rm` → resource
`"rm"`) and forced a growing alias table in its frontend. Grouping here is
explicit in the registry, never inferred.

## 3. Registry contents (as of 2026-09-03)

| Module | Keys | Notes |
|---|---|---|
| `orders` | `orders.sales.view / .create / .edit`, `orders.decision.simple`, `orders.status.transition`, `orders.mart.decide` | Gate the order lifecycle endpoints |
| `pages` | 16 legacy admin-page keys (`App_User`, `Sap_Sync`, `Reports`, …) | Verbatim from `Frontend/src/config/adminPages.ts` |
| `payments` | `Payments_Create/Approve`, `Deposit_Create/Approve`, `Payments_Dashboard` | Verbatim from `payments/permissions.py`; mobile checks these strings |
| `invoices` | `invoices.sales.create`, `invoices.review.decide`, `invoices.report.view` | Gate the invoice **routes** (frontend); backend invoice endpoints are still `IsAuthenticated` — see §9 |
| `tracker` | The 7 page keys from `ROLE_PAGE_MAP` (`Tracker_Entry`, …, `Ap_Invoice_Entry`) | Unioned with the sub-role map — see §6 |

**HANA has no module, deliberately**: `hana/` endpoints are read-only data
feeds behind pages already keyed (`Product_Stock`, `Reports`). If per-report
granularity is ever wanted, split the umbrella `Reports` key in `pages` with
a back-grant migration (the EXIM balance-sheet split is the model).

## 4. API

All under `/api/auth/`, all **admin-only** (`IsAdminRole`), JWT required.

### Registry & bundles

| Endpoint | Method | Purpose |
|---|---|---|
| `permission-registry/` | GET | The registry grouped by module: `{modules: [{name, keys: [{key, label}]}]}`. Static per deploy. |
| `roles/permissions/` | GET | Every role with `{id, name, display_name, is_active, keys, users}` plus a top-level `migrated` flag. `users` = distinct holders (primary ∪ extra). On an unmigrated DB: `migrated: false`, empty bundles, **no 500**. |
| `roles/<id>/permissions/` | PUT | `{"keys": [...]}` **replaces** the bundle (the matrix submits every box; merge would make unticking impossible). Unknown keys → 400 naming them. Unmigrated storage → 503 naming the fix. |

A bundle change takes effect on each holder's **next request** (server) — no
deploy, no re-login. The browser's copy refreshes on the next profile load.

### Role lifecycle

| Endpoint | Method | Purpose |
|---|---|---|
| `roles/create/` | POST | `{name, display_name}`. Name: 2–50 chars of `[A-Za-z0-9 _-]`, case-insensitively unique. |
| `roles/<id>/update/` | PUT | `display_name` and/or `is_active` only. |
| `roles/<id>/delete/` | DELETE | Deletes the role and (by cascade) its bundle. |

Guardrails, each returning an explanatory 400:

1. **`name` is immutable.** Fallbacks still match roles by name
   (`HasKeyOrRole`, `ROLE_PAGE_MAP`, order-desk scoping); renaming `billing`
   would silently strip that desk. Rename `display_name` instead.
2. **Privileged roles** (`core.permissions.PRIVILEGED_ROLE_NAMES`: `admin`,
   `tracker_admin`) cannot be deleted **or deactivated** — a UI slip must not
   lock every admin out.
3. **A held role cannot be deleted.** Checked across primary and
   `extra_roles`; the primary half is also enforced by the database
   (`on_delete=PROTECT`). The error names the holder count. Deactivate
   instead, or reassign holders first.

### Login / profile payload

`UserSerializer` now emits `permissions: [...]` — `sorted(effective_keys(user))`,
admin pre-expanded. **Additive**: clients that ignore it behave exactly as
before. Clients that read it (the web app does) get role bundles for free.

## 5. Enforcement classes (`core/permissions.py`)

| Class | Use | Status |
|---|---|---|
| `HasKey(key)` | The target state: `return [IsAuthenticated(), HasKey('orders.sales.create')]` in `get_permissions()`. Raises `ValueError` at construction for an unregistered key. | Permanent |
| `HasKeyOrRole(key, *roles)` | Key **or** legacy role admits. Exists so code deploy and `migrate` are order-independent: on an unmigrated DB the role half keeps every desk working; once seeded, both halves resolve the same people. | **Transitional — see cleanup contract §8** |
| `HasAnyRole(*names)` | Role-only gate through `has_role` (counts `extra_roles`). Superseded by the above. | Transitional |
| `IsAdminRole`, `IsAuthenticatedReadAdminWrite`, `IsSelfOrAdmin` | Pre-existing, unchanged. | Permanent |

Current `HasKeyOrRole` call sites (`orders/views/lifecycle.py`):

| Endpoint | Key | Fallback roles |
|---|---|---|
| `POST /orders/create/` | `orders.sales.create` | manager, billing, distributor |
| `PUT /orders/<id>/update/` | `orders.sales.edit` | creators + auditor |
| `POST /orders/<id>/approve|reject/` | `orders.decision.simple` | auditor, billing, manager, rate-approver spellings |
| `POST /orders/<id>/update-status/` | `orders.status.transition` | all order desks |

Helper functions converted to keys: `orders/views/mart.py:_is_mart_approver`
(`orders.mart.decide`), `orders/views/flow_config.py:_can_manage_order_flow`
(`Order_Flow_Settings`) — both also fixed to count `extra_roles` and use the
project-wide `is_admin`.

**Deliberately NOT converted:** the `role_name` reads in
`orders/views/_shared.py` (which orders a desk *sees* — workflow scoping) and
`orders/views/dashboards.py` (presentation branching). Those are not
authority and stay role-driven.

## 6. Tracker fold

`tracker_pages_for(user)` now returns
`ROLE_PAGE_MAP[roles] ∪ (effective_keys(user) ∩ ALL_TRACKER_PAGES)`.

* The four sub-roles keep meaning exactly what they meant.
* A tracker page can additionally be granted through any role's bundle or a
  personal grant — e.g. a manager gets `Tracker_Reports` without giving up a
  role slot.
* **POLICY CHANGE (deliberate, 2026-09-03):** admins now hold every tracker
  page. The backend previously excluded admins on purpose, but the frontend
  (`config/pageAccess.ts`) already showed admins every tracker link — they
  saw links that 403'd. The fold resolves the contradiction in favour of the
  system-wide rule "admin holds everything". To restore the exclusion,
  resolve tracker keys without the admin expansion in `tracker_pages_for`.

A test pins `REGISTRY['tracker'] == ALL_TRACKER_PAGES` so the two
vocabularies cannot drift.

## 7. Migrations & deploy order

| Migration | What | Reversible |
|---|---|---|
| `users/0031_role_permissions` | Creates `users_role_permissions` (schema only) | Yes |
| `users/0032_seed_role_permissions` | Seeds order-flow bundles from the Phase-1 role gates (billing/manager/distributor → `orders.sales.*`, etc.) | Yes — removes only seeded keys |
| `users/0033_seed_tracker_invoice_permissions` | Seeds tracker sub-role bundles (verbatim `ROLE_PAGE_MAP`) and invoice keys (billing, factory_approver) | Yes — removes only its keys |

Seeds **union** into existing bundles (admin edits survive re-runs) and match
role names case-insensitively (`Distributor`, `Mart_Approval` exist in data).
The `admin` role is never seeded — it holds everything implicitly.

**Deploy order does not matter.** Code before migrate: role fallbacks keep
every desk working, matrix page shows `migrated: false` and refuses saves
with instructions. Migrate before code: harmless extra table. This property
is the entire reason `HasKeyOrRole` exists.

```
python manage.py migrate users     # applies 0031 + 0032 + 0033
```

Seed contract (the EXIM-0010 pattern): **each role's bundle receives exactly
what the role already conferred — nobody's access changes on deploy day.**
Editing a bundle afterwards on the matrix page is a real change; that is the
point. A test (`core/tests_permission_registry.py`) pins every seeded key to
the registry so drift cannot silently strip a desk.

## 8. Cleanup contract (the debt this work took on, on purpose)

Once 0031–0033 are verified live (each desk's keys visible in their login
payload), in one pass:

1. Every `HasKeyOrRole(key, *roles)` call site drops to `HasKey(key)`.
2. `HasKeyOrRole` and `HasAnyRole` are **deleted**, with their tests
   (`HasKeyOrRoleTests` is annotated for exactly this).
3. The `has_role` fallback line in `_is_mart_approver` goes.
4. Frontend: the `roles:` halves of the converted `routeAccess.ts` entries
   (`/Add_Sales`, `/FOC`, `/View_Orders`, `/Drafts`, `/Sales_Invoice`,
   `/Invoice_Review`, `/Invoice_Report`) go, leaving `permissions:` only.

A fallback that outlives its migration is a second authority source — the
disease this work cures. Neither transitional class may gain call sites
beyond the migration.

## 9. Known gaps / follow-ups

* **Invoice backend endpoints are still `IsAuthenticated` only.** The
  `invoices.*` keys gate the frontend routes; mapping `invoice/views.py`'s
  15+ views to keys is its own pass (the orders treatment, repeated).
* **`_shared.py` / `dashboards.py`** still read the primary role FK alone for
  scoping/presentation — a known `extra_roles` blind spot, out of scope
  because it is not authority.
* **Live-DB staff-flag audit** still pending: on the TEST db, no auditor
  holds `is_staff`/`is_superuser` (which would bypass frontend gates via
  `isAdmin`), but `Guru` (approver, inactive) holds both and should lose
  them; live needs the same read-only check.
* **Suspicious grant found on TEST:** auditor `sir` holds `App_User` (user
  management), `Sap_Sync`, `Party_Product_Assignment`, `Order_Flow_Settings`.
  If live matches, revoke — `App_User` on an auditor is self-service
  privilege escalation.
* **User-model consolidation** (merge `role` FK + `extra_roles` into one
  `roles` M2M) is designed but not started; it removes the "consult BOTH
  fields" bug class at the root.

## 10. Frontend rendering chain

```
login/profile response .permissions
  → sessionFromApi(): grants = permissions ?? extra_pages   (auth/session.ts)
  → can(session, key)                                        (auth/permissions.ts)
     admins pass implicitly; fails closed with no session
  → ROUTE_ACCESS[path].permissions / .roles / .trackerPage   (auth/routeAccess.ts)
     unknown path ⇒ admin-only (fail closed)
  → ProtectedPage (route guard, part of the shell — cannot be forgotten)
  → Sidebar show(path) = canOpen(session, path)  (links ≡ reachable routes)
  → Guard / RequirePermission for in-page regions
```

None of this is a security boundary — every rule is enforced again
server-side. The client-side copy exists so users see an honest UI instead of
403s (see the NOT-A-SECURITY-BOUNDARY block in `auth/permissions.ts`).

### Admin screens

* **`/Role_Permissions`** (new, admin-only): create roles, rename display
  names, activate/deactivate, delete unused roles, and tick each role's
  permission bundle — rendered from the server's registry, so the page has no
  key list of its own to drift. Destructive buttons are disabled with
  tooltips mirroring the server guardrails.
* **`/Page_Permissions`** (existing): per-user grants (`extra_pages`) — now
  the *override* layer on top of role bundles. Any registry key is valid
  here, including the new `orders.*` / `invoices.*` / tracker keys.

## 11. How to: add a new module

1. Add its keys to `core/permission_registry.py` under a new module dict.
2. Gate its views: `return [IsAuthenticated(), HasKey('mymodule.thing.do')]`.
3. Add `routeAccess.ts` entries with `permissions: ["mymodule.thing.do"]`
   and a `MODULE_TITLES` label in `Role_Permissions.tsx`.
4. If existing roles should hold the keys from day one, write a seed
   migration in the 0032/0033 style (union, reversible, admin not seeded).

That is the whole checklist. There is no second mechanism.

## 12. Test map

* `core/tests_permission_registry.py` — registry integrity (no duplicate
  keys), `HasKey`/`HasKeyOrRole` construction-time failure, `effective_keys`
  fail-closed + degradation, seed-key drift pins, tracker vocabulary pin,
  DB-backed union/inert-key/admin tests.
* `payments/tests_permissions.py`, `core/tests.py` — pre-existing, unchanged.
* Note: the no-DB tests run standalone; the DB-backed ones need a test
  database (`manage.py test` creates one — coordinate with the no-migrations
  rule before running).
