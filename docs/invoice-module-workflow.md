# OMS-Backend `invoice` Module - Beginner Friendly Deep Notes (Express + DB lens)

`invoice` is OMS’s local ledger for invoice posting attempts and references to SAP draft/invoice artifacts.

It is a small ORM module:
- stores invoice payloads and outcomes
- stores reference logs (`ref_id`, status, posted user/time)
- does **not** directly call SAP by itself (that is done by Service Layer module)

## 1) Architecture in plain terms (Express view)

- `invoice/models.py` = table schema (`invoice_log`, `invoice_ref_logs`)
- `invoice/serializers.py` = request/response DTO (validation + shape)
- `invoice/views.py` = API handlers (create/list/create reference log)
- `invoice/urls.py` = route mapping
- `invoice/migrations/*.py` = table evolution history

Routes are included in Django project at:

- [OMS-Backend/OMS/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\OMS\urls.py) under `/api/invoice/`

## 2) Files in this module

- [invoice/models.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\invoice\models.py)
- [invoice/serializers.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\invoice\serializers.py)
- [invoice/views.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\invoice\views.py)
- [invoice/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\invoice\urls.py)
- [invoice/admin.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\invoice\admin.py)
- [invoice/migrations/*.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\invoice\migrations)

## 3) Tables and schema

Current tables (active model state):

- `invoice_log` (`InvoiceLog`)
- `invoice_ref_logs` (`InvoiceRefLogs`)

### `invoice_log`

Columns:
- `id` PK
- `so_number` (`CharField`)
- `party_name` (`CharField`)
- `total_amount` (`Decimal`)
- `ref_id` (`CharField`, nullable)
- `status` enum:
  - `PENDING`
  - `APPROVED`
  - `REJECTED`
  - `ERROR`
- `invoice_payload` (`JSONField`)
- `approved_by` (`FK users_user`, nullable)
- `rejected_by` (`FK users_user`, nullable)
- `rejection_reason` (`TextField`, nullable)
- `error_message` (`TextField`, nullable)
- `created_at` (`DateTime`, auto add)
- `created_by` (`FK users_user`)

There is custom `save()` behavior:
- if status `REJECTED` and no reason, fills `error_message` with `"Invoice rejected without specific reason."`
- if status `ERROR` and no error message, fills default error text.

### `invoice_ref_logs`

- `ref_id`
- `card_name`
- `doc_date`
- `so_number`
- `status`
- `error_message`
- `posted_by` (`FK users_user`)
- `posted_at` auto timestamp

## 4) Router map (`/api/invoice/`)

From [invoice/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\invoice\urls.py):

- `POST /api/invoice/log/create/` -> [InvoiceLogCreateView](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\invoice\views.py)
- `GET  /api/invoice/all/` -> [InvoiceLogListView](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\invoice\views.py)
- `POST /api/invoice/refLogs/` -> [InvoiceRefLogCreateView](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\invoice\views.py)

## 5) Endpoint workflow and query behavior

### 5.1 `POST /api/invoice/log/create/`

Flow:
- receives request payload
- validates with [InvoiceLogSerializer](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\invoice\serializers.py)
- `serializer.save(created_by=request.user)`
- writes one row into `invoice_log`

This is equivalent to:

```sql
INSERT INTO invoice_log (so_number, party_name, total_amount, ref_id, status, invoice_payload, created_by, ...)
VALUES (..., request.user.id, ...);
```

### 5.2 `GET /api/invoice/all/`

- optional query param `status`:
  - if present -> `WHERE status = :status`
  - else -> all rows
- returns serialized list.

### 5.3 `POST /api/invoice/refLogs/`

- inherited from DRF `CreateAPIView` with serializer+queryset
- inserts one row into `invoice_ref_logs`.

## 6) Permissions and auth behavior

Important: views import `IsAuthenticated` and `AllowAny` but do not currently enforce `permission_classes`.
So:
- permission depends on project defaults
- this is weaker than explicit per-endpoint protection in other modules.

Given `REST_FRAMEWORK.DEFAULT_AUTHENTICATION_CLASSES` is JWT-based in settings, requests still usually carry JWT but not enforced by class-level permission.

## 7) Migration timeline to understand evolution

The migration history includes:
- `0001_initial` creates `invoice_log` with a first version of status choices.
- `0002_invociehistory` creates a typo-table `invocie_history` (not used by current model set).
- `0003` adds `rejection_reason`.
- `0004` adds `invoice_ref_logs`.
- `0005` adds `approved_by`, `rejected_by`, `ref_id`; makes `error_message` nullable in `invoice_ref_logs`.
- `0006` removes typo table `InvocieHistory`.

If you see old DB artifacts, they come from intermediate migration steps.

## 8) ORM <-> SQL mental mapping

### Create invoice log

```sql
INSERT INTO invoice_log (
  so_number, party_name, total_amount, ref_id, status,
  invoice_payload, approved_by_id, rejected_by_id,
  rejection_reason, error_message, created_by_id, created_at
) VALUES (
  ..., ..., ..., ..., 'PENDING',
  'json_payload', ..., ..., ..., ..., :user_id, NOW()
);
```

### List logs

```sql
SELECT * FROM invoice_log
WHERE (:status IS NULL OR status = :status)
ORDER BY id DESC;
```

### Create reference log

```sql
INSERT INTO invoice_ref_logs (
  ref_id, card_name, doc_date, so_number, status, error_message, posted_by_id, posted_at
) VALUES (..., ..., ..., ..., ..., ..., :user_id, NOW());
```

## 9) How this module fits into OMS flow

Think of this as the local paper-trail layer:
- `orders` and `sap_sync` decide/prepare documents.
- `serviceLayer` sends invoice/draft payloads to SAP.
- `invoice` saves result snapshots/references for audit/retry visibility and user traceability.

The naming `ref_id` indicates cross-reference with SAP draft/reference values (`U_OMS_REF` usage in HANA queries, and related draft fetch APIs in [hana](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\docs\hana-module-workflow.md)).

## 10) What to check first when debugging

1. Confirm row appears in `invoice_log`:

```sql
SELECT id, so_number, status, created_by_id, created_at
FROM invoice_log
ORDER BY id DESC
LIMIT 20;
```

2. Check ref-id mapping:

```sql
SELECT ref_id, status, posted_by_id, posted_at
FROM invoice_ref_logs
WHERE ref_id = :ref_id;
```

3. For rejected/error cases:
- check `status`, `rejection_reason`, `error_message`
- confirm whether `created_by` user is present.

## 11) Notes for a new developer

- This module is intentionally thin; it is mostly “persist what happened” and keep UI-facing traceability.
- There is no complex document transformation logic here.
- If you need approval/rejection workflow APIs with status transitions, they are likely enforced in API consumers and not deeply implemented in this module.

Next modules still pending docs:
- [SKU module](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\docs\SKU-module-workflow.md) (to be created)
- [legal module](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\docs\legal-module-workflow.md) (to be created)
- [audit module](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\docs\audit-module-workflow.md) (to be created)
