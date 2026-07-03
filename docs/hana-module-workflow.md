# OMS-Backend `hana` Module - Beginner Friendly Deep Notes (Express + DB lens)

This note explains the `hana` module: live SAP/HANA lookups for inventory and sales operational data.

In this project, `hana` is mostly a **read-path** module (no local DB tables written from this app), used for real-time SAP validation and reporting style reads.

## 1) Architecture mapping for MERN/Express developers

- `hana/views.py` is the controller layer (Express route handlers).
- `hana/services/services.py` is the service layer.
- `hana/services/connection.py` is the database connector/query executor.
- `hana/utils.py` is response shaping (order/grouping helper).
- `hana/urls.py` is the router mapping.
- `hana/models.py` is present but not used (placeholder only).
- No local model tables are written by this module.

Mounted through:

- [OMS-Backend/OMS/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\OMS\urls.py) at `/api/hana/`.

## 2) Files in this module

- [hana/views.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\hana\views.py)
- [hana/services/services.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\hana\services\services.py)
- [hana/services/connection.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\hana\services\connection.py)
- [hana/utils.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\hana\utils.py)
- [hana/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\hana\urls.py)

## 3) What data this module reads from

There are no Django ORM models in this module (except an empty placeholder file).

The live reads go directly to SAP HANA tables via SQL in `Queries`, mainly:

- `OITM` item master
- `OITW` item warehouse stock
- `OWHS` warehouse master
- `RDR1` sales order lines
- `ORDR` sales order header
- `OCRD` business partner master
- `CRD1` addresses
- `OSLP` salesperson
- `OEXD` freight masters
- `ITM1` item price list
- `OIBT` batch/inventory
- `NNM1` number series and next doc series
- `ODRF` draft documents
- `OWDD` workflow draft status

The module uses one shared DB connection config:

- [OMS-Backend/OMS/settings.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\OMS\settings.py) `DATABASES['hana']`.

## 4) Route map (`/api/hana/...`)

- `GET /api/hana/product-stock/`
- `GET /api/hana/so/?card_code=<card_code>`
- `GET /api/hana/product-so/?item_code=<item_code>`
- `GET /api/hana/open-parties/`
- `GET /api/hana/customer-details/?card_code=<card_code>`
- `GET /api/hana/warehouse-details/?whs_code=<whs_code>`
- `GET /api/hana/salesperson-details/?slp_code=<slp_code>`
- `GET /api/hana/freight-masters/`
- `GET /api/hana/address/?card_code=<card_code>`
- `GET /api/hana/state-chain/?state_code=<state_code>`
- `GET /api/hana/vendor-states/`
- `GET /api/hana/all-customers/`
- `GET /api/hana/next-doc-number/?doc_type=<doc_type>`
- `GET /api/hana/fg-items/`
- `GET /api/hana/batch-details/?item_code=<item_code>&whs_code=<whs_code>`
- `GET /api/hana/inventory-details/?item_code=<item_code>`
- `GET /api/hana/item-price/?item_code=<item_code>&price_list=<price_list>`
- `GET /api/hana/series/?finYear=<finYear>&groupCode=<groupCode>`
- `GET /api/hana/draft/verify?refId=<refId>`
- `GET /api/hana/invoice-drafts/?statusCode=<statusCode>`

There are no POST/PUT/DELETE routes in `hana`.

## 5) Controller flow (`views.py`)

- Each handler validates required query params and returns `400` for missing required inputs.
- On valid input, controller calls one method on `SalesOrderService`.
- Response is directly returned, usually as raw list/json dict.
- Errors are converted to HTTP response (mainly default DRF `500` if exceptions bubble, except explicit param-check returns `400`).

Examples:

- `get` `/so/`:
  - validates `card_code`
  - calls `SalesOrderService().syncSalesOrder(card_code)`
  - calls `group_sales_orders(rows)` for grouped response
- `get` `/invoice-drafts/`:
  - validates `statusCode`
  - calls `SalesOrderService().get_invoice_status(statusCode)`
  - wraps in `{"data": ...}`

## 6) Service layer flow (`SalesOrderService`)

`SalesOrderService` method pattern is consistent:

- open connection via `with HANAConnection()`
- build query string from `Queries`
- execute via `conn.execute(query)`
- return list of dict rows

No business transformation is done except:

- stock queries return raw dict rows from SQL
- sales-order rows are grouped in `group_sales_orders`.

Important: this class uses many dynamic methods from `Queries` directly, so you can trace SQL logic in one place.

## 7) Connection design and SQL execution

From [hana/services/connection.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\hana\services\connection.py):

- `HANAConnection.connect()`:
  - reads `settings.DATABASES['hana']`
  - uses `hdbcli.dbapi.connect(address, port, user, password)`
  - creates cursor
- `execute(sql, params=None)`:
  - runs `cursor.execute(sql, params or [])`
  - if query returns rows, converts each row to dict keys by cursor metadata.
  - if non-select query, commits and returns `[]`
  - on SQL error, rolls back and raises `RuntimeError`.
- `__enter__` / `__exit__` manage connection lifecycle.

Query builder methods live in `Queries`:

- `get_product_stock()` has `UNION ALL` across company DBs and filters only open SO-related stock rows.
- `get_sales_orders_for_party(item)` and `get_sales_orders_for_product(item)` have open-sales-order filters and union across 3 company schemas.
- `get_next_doc_no`, `get_series`, `get_draft_verification`, and `get_invoice_status` support number/doc/draft related screens.

## 8) Key SQL behavior to understand

### 8.1 Multi-company support

HANA queries run against:

- `HANA_COMPANY_DB` (or base `SCHEMA`)
- `HANA_COMPANY_DB_BEVERAGES`
- `HANA_COMPANY_DB_MART`

`Queries._open_so_schemas()` builds a unique list and every open-sales-order query unions all schemas so one response can include cross-company pending demand.

### 8.2 Stock endpoint (`/product-stock/`)

Equivalent SQL pattern:

```sql
SELECT item + warehouse stock columns
FROM OITM
INNER JOIN OITW ON ...
INNER JOIN OWHS ON ...
INNER JOIN (
  SELECT ItemCode, WhsCode, SUM(OpenQty) AS RequiredQty
  FROM RDR1
  INNER JOIN ORDR ON ...
  WHERE CANCELED='N' AND DocStatus='O' AND LineStatus='O' AND OpenQty > 0
  GROUP BY ItemCode, WhsCode
) AS open_qty ON ...
WHERE ItemCode LIKE 'FG%' OR 'SCH%' OR ...
```

This means stock list is filtered only for items currently in open sales lines.

### 8.3 Open parties endpoint (`/open-parties/`)

Equivalent SQL pattern:

```sql
SELECT CardCode, CardName, COUNT(DocEntry) AS Num_of_Open_SalesOrder
FROM ORDR
WHERE DocStatus='O' AND CANCELED='N'
GROUP BY CardCode, CardName;
```

Then summed across all company DBs so one party appears once.

### 8.4 Sales orders drill-down (`/so/`, `/product-so/`)

Equivalent pattern:

```sql
SELECT ORDR.DocEntry, ORDR.DocNum, ORDR.CardCode, ORDR.DocDate, ..., RDR1.LineNum, RDR1.ItemCode, RDR1.OpenQty
FROM ORDR
INNER JOIN RDR1 ON ORDR.DocEntry = RDR1.DocEntry
WHERE CardCode OR ItemCode filter
  AND CANCELED='N'
  AND ORDR.DocStatus='O'
  AND RDR1.LineStatus='O'
  AND OpenQty > 0
```

Rows are grouped by document using `group_sales_orders`.

## 9) Response shape examples

### Product stock

Each row includes:

- `item_code`, `item_name`, `category`, `warehouse_code`, `warehouse_name`
- `on_hand`, `total_on_hand`
- `pending_required_qty`, `left_over_stock`
- `sal_factor2`, `tax_rate`, `brand`, `is_active`

### Sales order list (`/so/`, `/product-so/`)

- top-level order object fields: `DocEntry`, `DocNum`, `DocDate`, `DocDueDate`, `CardCode`, `DocTotal`, etc.
- `lines` array with line-level fields such as `ItemCode`, `Quantity`, `OpenQty`, `Price`, `LineTotal`, `WhsCode`.

### Draft + invoice lookups

- `/draft/verify?refId=...` returns ODRF fields filtered by `U_OMS_REF`.
- `/invoice-drafts/?statusCode=...` joins `OWDD` + `ODRF` and returns draft header details.

## 10) Module behavior in full workflow

`hana` is typically used by front-end screens or other backend modules requiring live SAP data, e.g.:

- live stock checks before allocation decisions
- open sales-order visibility
- getting next doc numbers and series
- looking up batch/warehouse/price before documents
- checking draft workflow state (for invoice and draft screens)

In `orders`, this service is also imported for two live HANA operations:

- quote document status checks by `DocEntry` in cancellation/approval flows
- draft verification support calls when needed by invoice/approval paths

## 11) Security and risk notes for new developers

- Permission classes are not explicitly set per view, so the module inherits project-wide DRF defaults.
- In this project, default permission class is not forced in settings, so these endpoints can become public if not protected at higher middleware level.
- Many SQL strings are built via f-strings with raw values. Even when values are simple IDs, this is SQL-injection-prone pattern.
- No local transaction writes or audit logs are performed inside this module.
- All failures bubble as standard 500 unless explicit 400 checks catch missing params.

## 12) Practical DB troubleshooting queries

- verify HANA connection config values from:
  - [OMS-Backend/OMS/settings.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\OMS\settings.py)
- test raw response quality for one party:
  - `SELECT * FROM "OCRD" WHERE "CardCode" = 'CUST_CODE'`
- test open sales order count:
  - `SELECT COUNT(*) FROM "ORDR" WHERE "CardCode"='CUST_CODE' AND "DocStatus"='O' AND "CANCELED"='N'`
- test item stock details:
  - `SELECT * FROM "OITW" WHERE "ItemCode" = 'FG...' AND "OnHand" > 0`

## 13) What to read next

Next modules still not documented in `docs` are:
- [invoice-module-workflow](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\docs\invoice-module-workflow.md) (does not exist yet)
- [serviceLayer-module-workflow](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\docs\serviceLayer-module-workflow.md) (does not exist yet)
- [SKU-module-workflow](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\docs\SKU-module-workflow.md) (does not exist yet)

If you want, we can create these next in the same exact structure.
