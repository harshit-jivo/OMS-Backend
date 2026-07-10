# OMS-Backend `audit` Module - Beginner-Friendly Deep Notes

This module records **who changed what** inside admin-related business data and keeps a durable history in PostgreSQL.

- `audit/models.py` defines `AuditLog` table.
- `audit/signals.py` captures model-level create/update/delete and M2M changes.
- `audit/middleware.py` sets request context and writes records at request end.
- `audit/context.py` stores per-request shared state (thread-safe).
- `audit/pages.py` maps API URLs / model names to human-readable page names.
- `audit/admin.py` exposes a read-only admin UI for logs.

Installed in Django:
- [OMS/settings.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\OMS\settings.py) -> includes `audit` in `INSTALLED_APPS` and `audit.middleware.AuditMiddleware` in `MIDDLEWARE`.
- `AuditConfig.ready()` wires signals when app starts.

No dedicated `audit/urls.py` exists. Logging is automatic side-effect.

## 1) Files in this module

- [audit/models.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\audit\models.py)
- [audit/context.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\audit\context.py)
- [audit/middleware.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\audit\middleware.py)
- [audit/signals.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\audit\signals.py)
- [audit/pages.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\audit\pages.py)
- [audit/apps.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\audit\apps.py)
- [audit/admin.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\audit\admin.py)

Migrations:
- [audit/migrations/0001_initial.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\audit\migrations\0001_initial.py)

## 2) Data model (`audit_log`)

Model: [AuditLog](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\audit\models.py)

- DB table: `audit_log`
- Columns:
  - `user` (`ForeignKey` to auth user, nullable, `SET_NULL`)
  - `username` (snapshot string, survives user deletion)
  - `page` (which admin page/API route)
  - `action` (`Created`/`Updated`/`Deleted`/etc.)
  - `record` (human-readable identifier, e.g. `User: johnd`)
  - `field` (comma-separated changed fields for multi-change rows)
  - `old_value` (text)
  - `new_value` (text)
  - `created_at` timestamp index
- Ordering: newest first (`['-created_at']`)

## 3) How audit gets triggered (execution flow)

### 3.1 App start -> signal registration

- On startup, Django loads `audit.apps.AuditConfig.ready()`.
- `ready()` calls `signals.connect()`.
- `connect()` resolves a fixed list of audited models and registers:
  - `pre_save` -> capture old values
  - `post_save` -> detect created/updated diffs
  - `post_delete` -> capture deletions
  - `m2m_changed` -> capture many-to-many add/remove for selected fields

Audited models list:
- `users.User`
- `users.UserPartyAssignment`
- `users.PartyProductAssignment`
- `users.SchemeProduct`
- `orders.OrderFlowConfig`
- `orders.PartyOrderFlowConfig`
- `sap_sync.SyncLog`

Audited M2M fields:
- `users.User.main_groups`
- `users.User.states`

### 3.2 Request entry -> request-level context

For every request, middleware checks:
- method is in `{POST, PUT, PATCH, DELETE}`.
- if yes, `context.begin(user,page)` stores:
  - current user (resolved from JWT if possible)
  - page label from URL/path
  - active flag + empty change buffer

`context` is thread-local so concurrent requests don’t mix.

### 3.3 Signal handlers collect changes

For audited model writes:
- `pre_save` stores old field values (`_audit_old`) for updates.
- `post_save` compares old vs new values:
  - ignores auto-managed timestamp + internal bookkeeping fields (`assigned_by`, `created_by`, `updated_by`)
  - emits one buffered change-set per record (`model:pk` key)
  - password fields are hidden in log (`(hidden)`)
- `post_delete` marks action `Deleted`.
- `m2m_changed` records added/removed related IDs for configured M2M fields.

### 3.4 Request exit -> flush

In `AuditMiddleware.__call__`, after `get_response`:
- calls `signals.flush()`: writes one `AuditLog` row per buffered record.
- if no buffered row was written and request was mutating + path mapped to admin page and status is success, writes one fallback row (`page`, generic action from HTTP method).
- clears context.

Result:
- One database row per changed record per request, even when multiple fields and model events happen together.
- For bulk updates not captured by model signals, fallback ensures no successful admin mutation disappears silently.

## 4) Page naming and action naming (for understanding logs)

- URL path mapping in [audit/pages.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\audit\pages.py):
  - `/auth/users/...` -> `App User`
  - `assign-parties` -> `Party Assignment`
  - `party-product` -> `Party Product Assignment`
  - `/sap/sync` -> `SAP Sync`
  - `flow-config` -> `Order Flow Settings`
  - ... etc.
- If path not recognized, fallback uses model->page map:
  - `users.User` -> `App User`
  - `sap_sync.SyncLog` -> `SAP Sync`
  - etc.
- Action mapping from HTTP method (in fallback path):
  - `POST` -> `Created`
  - `PUT` / `PATCH` -> `Updated`
  - `DELETE` -> `Deleted`

## 5) ORM SQL mental model

Signal writes are equivalent to:

- Insert audit row:
  - `INSERT INTO audit_log (user_id, username, page, action, record, field, old_value, new_value, created_at) VALUES (...)`
- No explicit `SELECT` endpoint is in this module; reads usually happen from admin UI.
- Admin list/filter page is built on Django ORM querysets over `audit_log` with filters on `page`, `action`, `created_at`.

## 6) Important workflow points in other modules

`audit` is mostly automatic, but some code also writes audit rows directly.

Example found:
- [orders/views.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\views.py) has explicit manual `AuditLog.objects.create(...)` when SAP quotation is cancelled.

This means:
- Some important non-admin/non-model events can still be tracked even without signal hooks.
- In audits, read both automatic signals and manual writes together.

## 7) Admin access

- [audit/admin.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\audit\admin.py) registers model in admin.
- Read-only list view (filters/search/order/read-only fields).
- No add/delete/edit from admin UI by design.

## 8) Common onboarding checks

1. Confirm middleware is active in [settings.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\OMS\settings.py).
2. Confirm signals are connected (run app once and inspect logs for warnings in `signals.connect()`).
3. Confirm your main admin routes are correctly matched in [pages.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\audit\pages.py) so `page` column is meaningful.
4. Use the admin logs UI (`AuditLog` model) for day-to-day troubleshooting.

## 9) Notes for MERN/Next.js comparison

- Think of `signals.py` as global model hooks (`pre_save`/`post_save`) that are globally attached like Mongoose middleware.
- Think of `middleware.py` + `context.py` as an Express-like request scope object where all model changes in one request are staged then committed once at response end.
- Think of `AuditLog` as an append-only `audit_logs` collection/table containing immutable snapshots of deltas.

## 10) Risks / caveats (important)

1. Because `audit` only listens to `AUDITED_MODELS`, changes in other tables are not auto-tracked.
2. Bulk `queryset.update()` (without save per row) won’t trigger signal diffs; fallback may still log one generic row if route is recognized and middleware is active.
3. For very large payload changes, full text values are truncated at ~5000 chars before storing.
4. JWT parse happens in middleware directly; request user context is best-effort.
5. There is no direct API endpoint to query `audit_log`; external consumers must use admin or new API you add.

