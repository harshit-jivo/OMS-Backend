# OMS-Backend `SKU` Module - Beginner-Friendly Deep Notes

This module manages OMS product image records (SKU catalog) used by internal tools and quality workflows.

- `SKU/models.py` stores one record per item code.
- `SKU/serializers.py` validates upload/list payloads.
- `SKU/views.py` exposes create/list/detail and pending-image endpoints.
- `SKU/urls.py` mounts these under `/api/sku/` in project routes.
- No core business workflow logic is here; this is mainly reference data + media management.

## 1) Files in this module

- [SKU/models.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\SKU\models.py)
- [SKU/serializers.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\SKU\serializers.py)
- [SKU/views.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\SKU\views.py)
- [SKU/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\SKU\urls.py)
- [SKU/apps.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\SKU\apps.py)
- Mounted in [OMS/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\OMS\urls.py):
  - `path('api/sku/', include('SKU.urls'))`

## 2) Purpose and place in architecture

- Stores `SKU` master rows and their image path for product items coming from integrations.
- Gives UI/admin pages a single API to:
  1. add a SKU with image,
  2. list all SKUs,
  3. read/update/delete one SKU by `item_code`,
  4. identify FG items that do not yet have image data.
- The `SKU` module is simpler than `orders`/`sap_sync`, but important as the shared image reference store.

## 3) Model and table design

- File: [SKU/models.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\SKU\models.py)
- Model: `SKU`
- DB table: `sku` (as defined by `Meta.db_table`)
- Key fields:
  - `item_code` (unique identifier, used as detail lookup key)
  - `item_name` (display text)
  - `item_image` (uploaded image path/file)
  - timestamp fields (`created_at`, `updated_at`)
- In SQL terms:
  - `item_code` is the business key and uniqueness guard.
  - `item_image` is nullable/empty when no image is uploaded.

## 4) Endpoints (`/api/sku/`)

From [SKU/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\SKU\urls.py):

- `POST /api/sku/upload/` -> `SKUCreateView` (create with image)
- `GET /api/sku/all/` -> `SKUListView` (read all rows)
- `GET /api/sku/<item_code>/` -> `SKUDetailView` (detail by `item_code`)
- `PUT /api/sku/<item_code>/` -> `SKUDetailView` (update fields, usually image/name)
- `PATCH /api/sku/<item_code>/` -> `SKUDetailView` (partial update)
- `DELETE /api/sku/<item_code>/` -> `SKUDetailView`
- `GET /api/sku/pending/` -> `SKUPendingList` (items missing local image while present as FG items from HANA)

## 5) Request/response flow by endpoint

### 5.1 `POST /api/sku/upload/`

Handler: `SKUCreateView`.

- Accepts multipart/form-data because image upload is involved.
- Serializer validates fields (`item_code`, `item_name`, image payload).
- On valid input:
  1. `serializer.save()` creates a `SKU` row via ORM.
  2. API returns the created object JSON with 201.
- On invalid data:
  - returns serializer error 400.

### 5.2 `GET /api/sku/all/`

Handler: `SKUListView`.

- Query all rows from `sku` and serialize as list.
- Straight read endpoint used by frontend dropdowns, admin tables, and bulk validations.

### 5.3 `GET /api/sku/<item_code>/`

Handler: `SKUDetailView` (`RetrieveUpdateDestroyAPIView` style DRF generic).

- Uses `item_code` as lookup field (not numeric PK).
- Typical ORM call: `SKU.objects.get(item_code=<item_code>)`.
- Supports:
  - retrieve current record,
  - update using PUT/PATCH,
  - delete.

### 5.4 `GET /api/sku/pending/`

Handler: `SKUPendingList`.

- Uses `SalesOrderService().getFGItems()` from SAP/HANA service layer wrapper.
- Gets list of FG items from live ERP side.
- Fetches existing local SKU codes with images from Django DB.
- Compares ERP FG codes against local catalog to return only “not yet uploaded locally” codes.
- This endpoint is useful for operations/content-prep teams to quickly identify image gaps.

## 6) ORM-style equivalent SQL (developer mental model)

These are the ORM patterns in SQL-style language:

- Create:
  - `INSERT INTO sku (...) VALUES (...)`
- Read all:
  - `SELECT * FROM sku`
- Read one:
  - `SELECT * FROM sku WHERE item_code = <code>`
- Update:
  - `UPDATE sku SET ... WHERE item_code = <code>`
- Delete:
  - `DELETE FROM sku WHERE item_code = <code>`
- Pending image logic:
  - `SELECT item_code FROM sku WHERE item_image IS NOT NULL AND item_image <> ''`
  - Compare with live FG list returned by `SalesOrderService().getFGItems()`

## 7) Notes for MERN/Next.js comparison

- Think of `SKU/models.py` as Mongoose schema + PostgreSQL model mapping (here Django ORM + SQL DB).
- `SKUPendingList` is similar to a computed endpoint in Node:
  - it does not only read one table,
  - it merges two data sources (ERP live + local DB).
- `item_code` is exactly like a unique business key in APIs you may have used in Express routes (e.g., `/sku/:itemCode`).

## 8) Integration points and dependencies

- No custom middleware or queue integration in this app.
- Relies on:
  - Django REST Framework for serialization + generic detail views,
  - media upload handling configured globally in project settings,
  - `sku` table migrations (standard Django migration pipeline),
  - `SalesOrderService` for ERP live comparison in pending flow.
- Common usage pattern:
  - front-end uploads image first (`/upload/`) for known item code,
  - pending endpoint ensures no live FG product is left un-image if required by ops policy.

## 9) What to check first as a new developer

1. Verify media storage path (`MEDIA_ROOT`, static serving) in settings.
2. Verify URL mount path exists in [OMS/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\OMS\urls.py).
3. Test one create/list/detail request and one `pending` comparison call with a populated ERP live list.
4. Confirm `item_code` uniqueness and existing front-end expectation (case sensitivity and string format).

