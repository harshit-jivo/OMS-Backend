# OMS-Backend `sap_sync` Module - Beginner Friendly Deep Notes (Express + DB lens)

This note documents how SAP master sync, SAP quotation push, and schedule execution work in the current codebase.

It is written for a MERN/Next.js background and maps each part to Express-like layers.

## 1) Module architecture in plain language

- `sap_sync/models.py` = Django models + table definitions (same layer as Prisma/Sequelize schema).
- `sap_sync/services/connection.py` = raw SQL connector to SAP HANA SQL layer (ODBC-style).
- `sap_sync/services/sync_service.py` = integration/business service (sync + quote push).
- `sap_sync/services/hana_service.py` = alternative Service Layer client for login + POST wrappers.
- `sap_sync/views.py` = API handlers (controllers).
- `sap_sync/serializers.py` = request/response DTO + validation shape.
- `sap_sync/urls.py` = route declarations under `/api/sap/`.
- `sap_sync/scheduler.py` = APScheduler job orchestration.
- `sap_sync/apps.py` = app bootstrap/startup hook that starts scheduler in main process only.
- `sap_sync/migrations/*.py` = DB schema history for logs/schedules.

Think of the mount point like Express:
- `OMS-Backend/OMS/urls.py` includes `path('api/sap/', include('sap_sync.urls'))`.
- So all routes in this module are prefixed by `/api/sap/`.

## 2) Files in this module

- [sap_sync/models.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\sap_sync\models.py)
- [sap_sync/views.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\sap_sync\views.py)
- [sap_sync/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\sap_sync\urls.py)
- [sap_sync/serializers.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\sap_sync\serializers.py)
- [sap_sync/apps.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\sap_sync\apps.py)
- [sap_sync/scheduler.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\sap_sync\scheduler.py)
- [sap_sync/services/connection.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\sap_sync\services\connection.py)
- [sap_sync/services/sync_service.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\sap_sync\services\sync_service.py)
- [sap_sync/services/hana_service.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\sap_sync\services\hana_service.py)
- [sap_sync/migrations/*.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\sap_sync\migrations)

## 3) Main database tables this module uses

From `sap_sync/models.py`:

- `sap_products` (`Product`)
- `sap_parties` (`Party`)
- `sap_party_addresses` (`PartyAddress`)
- `sap_sync_logs` (`SyncLog`)
- `sap_sync_schedules` (`SyncSchedule`)
- `sales_quotation_logs` (`SalesQuotationLog`)
- `branches` (`Branch`, configured as `managed = False`)

### Relationships and uniqueness behavior

- `Product` is unique on `(``item_code``, ``category``).
- `Party` is unique on `(``card_code``, ``category``).
- `PartyAddress` is unique on `(``card_code``, ``address_name``, ``category``).
- `Branch` is unique on `(``bpl_id``, ``category``).
- `SalesQuotationLog` stores `order_id` as string and is not FK-linked to `orders`.

Important: `PartyAddress` and `Party` are not defined with a real Django `ForeignKey` today; `card_code` is a normal string.  
So joins are done by value filters in queries (not DB FK relations).

## 4) Routing map (`/api/sap/...`)

From [sap_sync/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\sap_sync\urls.py), full list:

- `POST /api/sap/sync/all/`
- `POST /api/sap/sync/products/`
- `POST /api/sap/sync/parties/`
- `POST /api/sap/sync/addresses/`
- `POST /api/sap/sync/branches/`

- `GET /api/sap/products/`
- `GET /api/sap/products/<int:pk>/`
- `GET /api/sap/products/code/<str:item_code>/`

- `GET /api/sap/parties/`
- `GET /api/sap/parties/<int:pk>/`
- `GET /api/sap/parties/code/<str:card_code>/`

- `GET /api/sap/addresses/`
- `GET /api/sap/branches/`
- `GET /api/sap/logs/`
- `GET /api/sap/quotation-log/<int:order_id>/`

- `GET /api/sap/schedules/`
- `POST /api/sap/schedules/`
- `GET /api/sap/schedules/<int:pk>/`
- `PUT /api/sap/schedules/<int:pk>/`
- `DELETE /api/sap/schedules/<int:pk>/`
- `POST /api/sap/schedules/<int:pk>/toggle/`

- `GET /api/sap/status/`
- `POST /api/sap/push-quotation/`
- `POST /api/sap/approve-order/`
- `GET /api/sap/parties/category/?category=<name>`

- `POST /api/sap/test-quotation/`
- `POST /api/sap/test-quotation/<int:pk>/` (note: `<pk>` is defined but not used in current code)

Most handlers are currently using `AllowAny`; one endpoint uses `IsAuthenticated`:
- `GET /api/sap/quotation-log/<order_id>/`

## 5) Read/write behavior by endpoint group

### 5.1 Manual sync triggers (data fetch from SAP DB)

These POST endpoints call [SyncService](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\sap_sync\services\sync_service.py) with `triggered_by='manual'` and return structured sync results:

- `POST /sync/all/` → `SyncService.sync_all()`
- `POST /sync/products/` → `SyncService.sync_products()`
- `POST /sync/parties/` → `SyncService.sync_parties()`
- `POST /sync/addresses/` → `SyncService.sync_party_addresses()`
- `POST /sync/branches/` → `SyncService.sync_branches()`

Each successful run creates/updates a row in `sap_sync_logs`.

### 5.2 Product endpoints

All reads from `Product` table.

- `GET /products/` supports query params:
  - `category` (`filter category__iexact`)
  - `search` (`item_code` OR `item_name` ILIKE)
  - `brand` (`brand__icontains`)
  - `exclude_deleted` (`true|false`, default true, filters `is_deleted!='Y'`)
- `GET /products/<int:pk>/` gets by ID
- `GET /products/code/<str:item_code>/` gets by `item_code`

Also applies `active_product_q()` i.e. `is_active` equals `Y` (case-insensitive), except there is no explicit active filter on every method due route-level querysets.

### 5.3 Party endpoints

- `GET /parties/` list filters:
  - `search` on `card_code` or `card_name`
  - `state` (`state__icontains`)
  - `main_group` (`main_group__icontains`)
  - `card_type`

- `GET /parties/<int:pk>/` and `GET /parties/code/<card_code>/` return one party (with `addresses` nested serializer field in `PartySerializer`).

### 5.4 Party address endpoints

- `GET /addresses/` list filters:
  - `card_code`
  - `address_type`
  - `gst` (`gst_number__icontains`)

### 5.5 Branches

- `GET /branches/` returns all rows from `Branch` with ordering `category, bpl_id`.
- `POST /sync/branches/` updates branch table from SAP query.

### 5.6 Logs

- `GET /logs/?sync_type=&status=&limit=` returns recent log rows from `sap_sync_logs`.
- `GET /quotation-log/<order_id>/` returns latest successful `SalesQuotationLog` with `order_id`, SAP doc fields and created time.

### 5.7 Scheduler CRUD

- `GET /schedules/` list all
- `POST /schedules/` create one (`sync_type`, `frequency`, `custom_interval_minutes`, `hour`, `is_active`)
- `GET/PUT/DELETE /schedules/<pk>/` detail operations
- `POST /schedules/<pk>/toggle/` flips `is_active`

## 6) Sync internals (service layer behavior)

### 6.1 `sync_all()` orchestration

`SyncService.sync_all()` executes, in order:
1) `sync_products()`  
2) `sync_parties()`  
3) `sync_party_addresses()`  
4) `sync_branches()`  

It keeps totals (`processed/created/updated`) and accumulates errors.

### 6.2 `sync_products()` workflow

SQL source method: [SAPConnection.get_products_query](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\sap_sync\services\connection.py#L77).

For each row:
- Reads fields from row names like:
  - `ItemCode`, `ItemName`, `Category`, `SalFactor2`, `U_Rev_tax_Rate`, `Deleted`,
    `U_Variety`, `SalPackUn`, `U_Brand`, `OnHand`, `validFor`
- `update_or_create(item_code, category, defaults=...)`
- Increments:
  - `created_count` when new
  - `updated_count` when matched existing

### 6.3 `sync_parties()` workflow

SQL source method: [SAPConnection.get_parties_query](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\sap_sync\services\connection.py#L126).

For each row:
- Uses `CardCode` + `Category` uniqueness key.
- Maps `CardType` and other master columns into `Party`.

### 6.4 `sync_party_addresses()` workflow

SQL source method: [SAPConnection.get_party_addresses_query](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\sap_sync\services\connection.py#L181).

For each row:
- `update_or_create(card_code, address_name, category, defaults=...)`
- Captures row-level errors without stopping entire loop.
- `row_errors` are attached to `SyncLog.error_message` (first 25 errors).

### 6.5 `sync_branches()` workflow

SQL source method: [SAPConnection.get_branches_query](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\sap_sync\services\connection.py#L245).

For each row:
- `update_or_create(bpl_id, category, defaults=bpl_name)`
- Writes to `branches` table with `managed=False`.

### 6.6 SyncLog write pattern

Every sync method creates a row in `sap_sync_logs`:
- `status`: `STARTED`
- on success: `SUCCESS`, plus counters, `completed_at`
- on fail: `FAILED`, with `error_message`

`SyncLogSerializer` also adds `duration` (seconds between `started_at` and `completed_at`).

## 7) SAP connection layer (`connection.py`)

`SAPConnection` encapsulates SQL endpoint to HANA via `pymssql`.

Important behavior:
- reads config from settings with safe defaults:
  - host: `SAP_DB_HOST` default `103.89.45.75`
  - port: `SAP_DB_PORT` default `1433`
  - db: `SAP_DB_NAME` default `Jivo_All_Branches_Live`
  - credentials from settings
- tries connection in 3 forms:
  1) `host,port`
  2) `host:port`
  3) separate `host` + `port` parameter
- executes raw SQL via `cursor(as_dict=True)`.

Query builders included:
- `get_products_query()`
- `get_parties_query()`
- `get_party_addresses_query()`
- `get_branches_query()`
- `get_live_stock_query(category, item_codes)` (not called from sync handlers in this file, but available for stock checks elsewhere)

### SQL query shape (conceptual)

All major methods are `UNION ALL` over category databases:
- `OIL` (category code `OIL`)
- `BEVERAGES` (category code `BEVERAGES`)
- `MART` (category code `MART`)

So each master sync pulls combined result sets from three SAP sources in one result set.

## 8) SAP quotation push workflow (`create_sales_quotation`)

This is the outbound integration flow to SAP Service Layer.

Trigger points:
- `POST /api/sap/approve-order/` (loads `Order` by `order_id`, calls `create_sales_quotation`)
- `POST /api/sap/push-quotation/` (same call, simpler response envelope)
- `POST /api/sap/test-quotation/` (uses `SimpleNamespace` mock payload flow)

Core mapping steps:
1) Build payload with `map_order_to_sap(order)`:
   - Reads `order.items.all()`
   - quantity from `qty`, fallback to `boxes`
   - price from `market_price` if > 0 else `basic_price`
   - resolves company database by item categories:
     - if mixed categories → default DB
     - if all `BEVERAGES` → beverage DB setting
2) Add normal product lines.
3) Add scheme/free item lines when item has scheme metadata:
   - reads related scheme IDs from `item.schemes` or fallback helper
   - resolves scheme item codes and adds zero-unit-price scheme components
4) POST to `"{HANA_SERVICE_LAYER_URL}/Quotations"` with SSL fallback handling.
5) On `201`:
   - creates/upgrades `SalesQuotationLog`:
     - `status = SUCCESS`
     - stores `DocEntry` and `DocNum`
   - marks `order.status = 6`
   - marks `order.sap_created = True`
6) On non-201 or exception:
   - `SalesQuotationLog.status = FAILED`
   - stores raw error text in `error_message`.

## 9) Scheduler workflow (`scheduler.py`)

### Start behavior

- App startup hook in [apps.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\sap_sync\apps.py):
  - when `RUN_MAIN == 'true'`, calls `start_scheduler()`.
- `start_scheduler()`:
  - reads all active schedules (`SyncSchedule.objects.filter(is_active=True)`)
  - calls `add_schedule_job()` each
  - starts APScheduler.

### Job behavior

- `run_scheduled_sync(schedule_id)`:
  - fetches schedule row
- if inactive => skip
- calls service method based on `sync_type`:
  - `ALL` / `PRODUCT` / `PARTY` / `PARTY_ADDRESS` (`BRANCH` falls through to ALL currently)
- sets `last_run = now()` on schedule

### Trigger mapping

- `HOURLY` => every 1 hour (`IntervalTrigger(hours=1)`)
- `DAILY` => every day at `hour:00` (`CronTrigger(hour=schedule.hour)`)
- `WEEKLY` => every Monday at `hour:00`
- `CUSTOM` => every `custom_interval_minutes`
- fallback => every 24h

### Note on live changes

`refresh_schedules()` exists and can remove/re-add jobs, but none of the API endpoints currently call it directly.
That means create/update/toggle/delete API actions may not immediately rewire APScheduler in a currently running process unless restart/reload occurs.

## 10) Express-style mapping for new developers

In Express terms:
- `urls.py` = router registrations
- `views.py` classes = controller handlers
- `SyncService` methods = service layer
- `SAPConnection` / `HANAServiceLayer` = integration clients
- `models.py` + migrations = DB schema
- `serializer` fields = DTO/output schema

## 11) Actual SQL-style read/write understanding

### A) Sync read/update pattern

```sql
BEGIN TRANSACTION
FOR each row returned from SAPQuery:
  SELECT * FROM sap_products WHERE item_code = :ItemCode AND category = :Category;
  IF not found:
    INSERT INTO sap_products (...) VALUES (...)
  ELSE
    UPDATE sap_products
    SET item_name = :ItemName,
        sal_factor2 = :SalFactor2,
        tax_rate = :U_Rev_tax_Rate,
        is_deleted = :Deleted,
        on_hand = :OnHand,
        is_active = :validFor,
        ...
    WHERE item_code = :ItemCode AND category = :Category;
END
COMMIT
```

This is exactly what `update_or_create` gives you in each sync method.

### B) Log insert/update pattern

```sql
INSERT INTO sap_sync_logs (sync_type, status, triggered_by, started_at)
VALUES (:sync_type, 'STARTED', :triggered_by, NOW());

UPDATE sap_sync_logs
SET status = 'SUCCESS' OR 'FAILED',
    records_processed = :count,
    records_created = :count,
    records_updated = :count,
    error_message = :err,
    completed_at = NOW()
WHERE id = :id;
```

### C) SAP quotation call pattern

```sql
-- pseudo payload structure
INSERT/POST payload to Service Layer /Quotations:
{
  "CardCode": "...",
  "DocDate": "...",
  "DocumentLines": [
    {"ItemCode": "...", "Quantity": ..., "UnitPrice": ...},
    ...
  ]
}
```

No DB SQL write for SAP; response log writes:

```sql
INSERT INTO sales_quotation_logs (
  order_id, status, request_data, response_data, sap_doc_num, sap_doc_entry, completed_at
) VALUES (:order_id, 'SUCCESS', ..., ..., :DocNum, :DocEntry, NOW());
```

## 12) Querying DB directly while onboarding

From `sap_sync` perspective, quick checks:

- list last product syncs:

```sql
SELECT * FROM sap_sync_logs
ORDER BY started_at DESC
LIMIT 20;
```

- verify party data shape per category:

```sql
SELECT card_code, card_name, state, main_group, category
FROM sap_parties
WHERE category IN ('OIL','BEVERAGES','MART')
ORDER BY category, card_code
LIMIT 100;
```

- verify quotation output:

```sql
SELECT order_id, status, sap_doc_num, sap_doc_entry, created_at
FROM sales_quotation_logs
WHERE order_id = :order_id
ORDER BY created_at DESC;
```

### Important project-note (Node/Express analogy)

Think of this as:
- `sync_*` endpoints = admin-only internal jobs in MERN (e.g., cron + API control panel)
- `sync_*` methods = job workers that read from external DB and upsert local collections
- `logs` endpoints = observability table reads
- `approve/push quotation` = synchronous webhook-like integration call to SAP

## 13) Security/behavior notes for a new developer

Current notable behavior to be aware of:
- Many write endpoints are `AllowAny` (especially sync triggers and quotation operations).
- `sync_type` in schedule includes `PARTY_ADDRESS`, `PRODUCT`, `PARTY`, and `ALL`; `BRANCH` falls into default path in current switch.
- `test-quotation/<int:pk>/` route exists but does not consume `<pk>`.
- `refresh_schedules()` is not auto-invoked from CRUD views.
- `PartySerializer.addresses` expects related address data but `PartyAddress` has no real FK relation to `Party`.

## 14) Other module links for onboarding context

- `orders` uses `sap_sync` data for item/party/address lookup and for quotation flow:
  - [orders/views.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\views.py)
- `users` drives party/product assignment data used during scheme pricing in sync service:
  - [users/models.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\users\models.py)
- `orders/scheme_rules.py` contributes scheme helper functions used by quote mapping:
  - [orders/scheme_rules.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\scheme_rules.py)

Next module docs can follow this same template:
- `serviceLayer` if exists as separate app
- `payment` / `notifications` / `storage` style modules you want next.
