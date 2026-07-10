# OMS Backend Developer Onboarding Checklist

> Purpose: single source doc for a new backend developer moving from MERN/Next.js to this Django + DRF project.

Generated files:
- OMS-backend project root: `OMS-Backend`
- API base URL (local): `http://127.0.0.1:8000`
- Route prefix mapping: `/api/auth`, `/api/orders`, `/api/sap`, `/api/hana`, `/api/service-layer`, `/api/invoice`, `/api/sku`, `/api/legal`

## 1) Exact setup checklist (run in order)

1. Open repo
```powershell
cd C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend
```

2. Create venv + activate
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

3. Install dependencies
```powershell
pip install --upgrade pip
pip install -r requirements.txt
```

4. Prepare environment
- Prefer local copy:
```powershell
Copy-Item .env .env.local
```
- Use either `.env` or `.env.local` and point process env accordingly.

5. DB bootstrap (PostgreSQL)
```powershell
python manage.py migrate
```

6. Create superuser (for `/admin/`)
```powershell
python manage.py createsuperuser
```

7. Run server
```powershell
python manage.py runserver
```

8. Verify health routes manually in browser / Postman
- `GET /api/auth/profile/`
- `GET /api/orders/status/`
- `GET /api/sap/status/`

9. Optional data sync for local test content
```powershell
# full SAP sync
POST http://127.0.0.1:8000/api/sap/sync/all/
```

10. Keep credentials and SAP tokens out of git. Use `.gitignore` for env files.

---

## 2) Required environment variables

| Env | Purpose | Required |
|---|---|---|
| `DB_NAME` | PostgreSQL DB name | Yes |
| `DB_USER` | PostgreSQL user | Yes |
| `DB_PASSWORD` | PostgreSQL password | Yes |
| `DB_HOST` | PostgreSQL host | Yes |
| `DB_PORT` | PostgreSQL port | Yes |
| `SECRET_KEY` | Django secret (currently hard-coded fallback exists) | No in code, but yes for production security |
| `DEBUG` | Django debug mode | Yes |
| `ALLOWED_HOSTS` | Hosts allowed in production | Yes |
| `HANA_DB_HOST` | HANA database host | Yes |
| `HANA_DB_PORT` | HANA database port | Yes |
| `HANA_DB_NAME` | HANA schema | Yes |
| `HANA_DB_USER` | HANA DB user | Yes |
| `HANA_DB_PASSWORD` | HANA DB password | Yes |
| `HANA_SERVICE_LAYER_URL` | SAP Service Layer base URL | Yes |
| `HANA_USERNAME` | SAP Service Layer username | Yes |
| `HANA_PASSWORD` | SAP Service Layer password | Yes |
| `HANA_COMPANY_DB` | SAP company code used in payload shaping | Yes |
| `HANA_COMPANY_DB_BEVERAGES` | Optional separate company for beverage flow | No |
| `HANA_WAREHOUSE_CODE` | HANA warehouse code | No |
| `HANA_WAREHOUSE_CODE_BEVERAGES` | Beverage warehouse code | No |
| `HANA_SSL_VERIFY` | SSL verify boolean for SAP calls | No |
| `HANA_SSL_CA_BUNDLE` | SSL cert path for production | No |
| `HANA_CONNECT_TIMEOUT` | HTTP connect timeout in seconds | No |
| `HANA_READ_TIMEOUT` | HTTP read timeout in seconds | No |
| `SAP_DB_HOST` | SAP sync SQL host | Yes (if using sap_sync)
| `SAP_DB_PORT` | SAP sync SQL port | Yes |
| `SAP_DB_NAME` | SAP sync SQL DB name | Yes |
| `SAP_DB_USER` | SAP sync SQL user | Yes |
| `SAP_DB_PASSWORD` | SAP sync SQL password | Yes |
| `SAP_APPROVER_USER` | Approver credential for SAP actions | Yes |
| `SAP_APPROVER_PASSWORD` | Approver password for SAP actions | Yes |

Notes:
- `settings.py` has `DEBUG` parsed from env and then overridden to `True`, so check production hardening before deployment.
- `CORS_ALLOW_ALL_ORIGINS` and `HANA_SSL_VERIFY` defaults are permissive during development.

---

## 3) Gotchas / risks you must check first (MERN-friendly checklist)

1. Authentication is partly configured:
- DRF uses JWT auth class globally.
- Default permission class is not strictly global; many endpoints are effectively open (`AllowAny`).
- For safety, wrap sensitive views in `permission_classes=[IsAuthenticated]` if exposing externally.

2. Security defaults are insecure for production:
- `DEBUG` forced `True` in settings.
- Hard-coded `SECRET_KEY` present.
- Several SAP calls may use certificate bypass behavior in code paths.

3. SAP session/state coupling:
- Service-layer cache keys like `b1_session` and `route_id` are reused; this can leak cross-user behavior under concurrency.

4. Non-strict DB relationships:
- Many joins between PostgreSQL and SAP/HANA data are done in Python/app logic using `card_code`, `item_code`, `order_id` string mappings.

5. Async work is mostly synchronous:
- Document parsing / SAP calls can block worker thread (especially legal upload and SAP sync calls).

6. Parser behavior:
- In legal endpoint, parser config may be configured with singular property forms, verify DRF compatibility before relying on file upload behavior.

---

## 4) Quick module architecture map (Express.js mental model)

- `users` = auth, user directory, RBAC-like mappings, party/product access mapping.
- `orders` = main business domain (create order, items, approval, status transitions, logs, notifications, dashboard).
- `sap_sync` = sync jobs + SAP master data cache + sync/audit schedules.
- `hana` = live reads from HANA DB (stock, SO, docs) via raw SQL-style service class.
- `serviceLayer` = direct SAP Service Layer proxy endpoints.
- `invoice` = lightweight invoice log storage/retrieval.
- `SKU` = file + reference master for item images/labels.
- `legal` = upload endpoint for legal label parsing and AI extraction.
- `audit` app exists with simple audit log model for operation history.

Express analogy:
- `views.py` ˜ controllers
- `models.py` ˜ Mongoose/Prisma models
- `serializers.py` ˜ DTO validation layer (body schema)
- `urls.py` ˜ route definitions (`router.get/post/...`)
- `services`/`views` service calls ˜ async service functions

---

## 5) Postman-style endpoint matrix (all modules)

Base: `{{baseUrl}} = http://127.0.0.1:8000`

### 5.1 Auth module (`/api/auth/`)

| Method | URL | Body / Query | Typical request payload | Typical response |
|---|---|---|---|---|
| POST | `/api/auth/login/` | none | `{ "username": "..", "password": ".." }` | `{ token, user }` |
| GET | `/api/auth/profile/` | none | - | `{ id, username, role, company, main_group, state, page_permissions }` |
| GET | `/api/auth/states/` | none | - | `[{id, state_name}]` |
| GET | `/api/auth/companies/` | none | - | `[{id, name}]` |
| GET | `/api/auth/mainGroup/` | none | - | `[{id, name}]` |
| GET | `/api/auth/categories/` | none | - | `[{id, name}]` |
| GET | `/api/auth/roles/` | none | - | `[{id, name}]` |
| POST | `/api/auth/users/create/` | JSON | user registration object | created user |
| GET | `/api/auth/users/list/` | none | - | user list |
| GET | `/api/auth/users/<user_id>/` | none | - | user detail |
| PUT | `/api/auth/users/<user_id>/` | JSON | partial user fields | updated user |
| POST | `/api/auth/users/<user_id>/delete/` | none | - | `{ status }` |
| GET | `/api/auth/users/<user_id>/page-permissions/` | none | - | `{ extra_pages: [...] }` |
| GET | `/api/auth/users/<user_id>/parties/` | `?card_code=&category=` | - | parties assigned to user |
| GET | `/api/auth/parties/<card_code>/users/` | `?category=` | - | users mapped to party |
| POST | `/api/auth/assign-parties/` | JSON | `{ user_id, card_codes:[], category }` | `{ added, skipped, errors }` |
| POST | `/api/auth/assign-parties/bulk-upload/` | JSON | `[{user_id, card_code, category}]` | bulk summary |
| POST | `/api/auth/remove-party/` | JSON | `{ user_id, card_code, category }` | `{ removed: true }` |
| GET | `/api/auth/parties/<card_code>/products/` | `?category=` | - | party product list |
| POST | `/api/auth/party-product/add/` | JSON | `{card_code,item_code,category,basic_rate}` | upsert result |
| POST | `/api/auth/party-product/bulk-add/` | JSON | `{ card_code, products:[...] }` | upsert summary |
| POST | `/api/auth/party-product/update-rate/` | JSON | `{ card_code,item_code,category,basic_rate }` | `{ updated: true }` |
| POST | `/api/auth/party-product/remove/` | JSON | `{ card_code,item_code,category }` | `{ removed: true }` |
| POST | `/api/auth/bulk-party/assign-products/` | JSON | `{ card_codes:[...], products:[...] }` | mapping summary |

### 5.2 Orders module (`/api/orders/`)

| Method | URL | Body / Query | Typical request payload | Typical response |
|---|---|---|---|---|
| GET | `/api/orders/parties/` | filters | - | parties for selected context |
| GET | `/api/orders/dispatches/` | none | - | dispatch location list |
| GET | `/api/orders/addresses/` | `?card_code=&address_type=` | - | party address list |
| GET | `/api/orders/product-filters/` | `?category=&brand=&variety=` | - | filter dictionary |
| GET | `/api/orders/products/` | `?category=&brand=&variety=&search=` | - | product list |
| GET | `/api/orders/flow-config/` | `?flow_type=` | - | current flow config |
| POST | `/api/orders/flow-config/` | JSON | flow config payload | saved config |
| GET | `/api/orders/party-flow-config/` | `?state_code=` | - | party-specific flow rules |
| POST | `/api/orders/party-flow-config/` | JSON | override object | saved override |
| GET | `/api/orders/branch/` | none | - | branch list |
| GET | `/api/orders/status/` | none | - | order status master |
| GET | `/api/orders/schemes/` | none | - | scheme catalog |
| GET | `/api/orders/dashboard/` | `?year=&month=` | - | dashboard cards |
| GET | `/api/orders/dashboard/charts/` | `?from=&to=` | - | chart datasets |
| GET | `/api/orders/dashboardW/` | `?year=&month=` | - | alternate dashboard |
| GET | `/api/orders/dashboardW/charts/` | `?year=&month=` | - | chart dataset |
| GET | `/api/orders/status-tracking/` | none | - | order tracking list |
| POST | `/api/orders/create/` | JSON | order header + items | `{ id, status, total_amount }` |
| GET | `/api/orders/list/` | query filters (`status`, `user_id`, `state_code`) | - | filtered orders |
| PUT | `/api/orders/<order_id>/update/` | JSON | order update payload | updated order |
| DELETE | `/api/orders/<order_id>/delete-draft/` | none | - | delete confirmation |
| POST | `/api/orders/<order_id>/approve/` | none | - | status changed, log entry |
| POST | `/api/orders/<order_id>/reject/` | none | - | status changed, log entry |
| POST | `/api/orders/<order_id>/update-status/` | JSON | `{ status, remark, note }` | status transition result |
| GET | `/api/orders/party-products/<card_code>/` | none | - | price-mapped product list |
| GET | `/api/orders/<int:order_id>/orderlogs/` | none | - | workflow history |
| GET | `/api/orders/orderdetails/<order_id>/` | none | - | full order detail |
| GET | `/api/orders/orderdetailsbyid/<order_id>/` | none | - | same as above |
| GET | `/api/orders/ordersbyuser/<user_id>/` | none | - | orders by creator |
| POST | `/api/orders/create-scheme/` | JSON | scheme object | scheme created |
| GET | `/api/orders/templates/parties/` | none | - | template party list |
| GET | `/api/orders/templates/orders/` | query filters | - | template order list |
| GET | `/api/orders/stock-check/` | none | - | stock check result |
| GET/POST | `/api/orders/staff-products/` | query or body | GET: none, POST: `{ products: [...], removed_products: [...] }` | staff price/product mapping |
| GET | `/api/orders/notifications/` | none | - | notification list |
| POST | `/api/orders/notifications/` | JSON | notification object | created notification |
| PATCH | `/api/orders/notifications/<pk>/` | JSON | partial update fields | updated notification |
| POST | `/api/orders/push-token/` | JSON | `{ token, user_id, platform }` | token store result |
| GET | `/api/orders/quotation-status/` | `?order_ids=` | - | mapping `{order_id:status}` |
| GET | `/api/orders/quotation-overview/` | none | - | quotation aggregate state |
| POST | `/api/orders/<order_id>/cancel-quotation/` | none | - | cancellation result |
| POST | `/api/orders/api/ai-order-summary/` | JSON | order summary payload | AI text/JSON summary |

### 5.3 SAP sync module (`/api/sap/`)

| Method | URL | Body / Query | Typical request payload | Typical response |
|---|---|---|---|---|
| POST | `/api/sap/sync/all/` | none | - | `{ success, message, counts }` |
| POST | `/api/sap/sync/products/` | none | - | `{ success, sync_count }` |
| POST | `/api/sap/sync/parties/` | none | - | `{ success, sync_count }` |
| POST | `/api/sap/sync/addresses/` | none | - | `{ success, sync_count }` |
| POST | `/api/sap/sync/branches/` | none | - | `{ success, sync_count }` |
| GET | `/api/sap/status/` | none | - | `{ pending, completed, failed }` |
| GET | `/api/sap/products/` | query filters | - | product array |
| GET | `/api/sap/products/<pk>/` | none | - | product detail |
| GET | `/api/sap/products/code/<item_code>/` | none | - | product by item code |
| GET | `/api/sap/product-varieties/` | none | - | grouped variety map |
| GET | `/api/sap/parties/` | query filters | - | party array |
| GET | `/api/sap/parties/<pk>/` | none | - | party detail |
| GET | `/api/sap/parties/code/<card_code>/` | none | - | party by code |
| GET | `/api/sap/parties/category/?category=` | query | - | parties by category |
| GET | `/api/sap/addresses/` | query filters | - | address array |
| GET | `/api/sap/branches/` | none | - | branch array |
| GET | `/api/sap/logs/` | query filters | - | sync logs |
| GET | `/api/sap/quotation-log/<order_id>/` | none | - | last quotation log |
| GET | `/api/sap/schedules/` | none | - | schedule list |
| POST | `/api/sap/schedules/` | JSON | schedule payload | created schedule |
| GET | `/api/sap/schedules/<pk>/` | none | - | schedule detail |
| PUT | `/api/sap/schedules/<pk>/` | JSON | schedule payload | updated schedule |
| DELETE | `/api/sap/schedules/<pk>/` | none | - | removed |
| POST | `/api/sap/schedules/<pk>/toggle/` | none | - | toggled schedule |
| POST | `/api/sap/push-quotation/` | JSON | quotation payload from order | SAP push response |
| POST | `/api/sap/test-quotation/` | JSON/none | sample order payload | test response |
| POST | `/api/sap/test-quotation/<pk>/` | none | path order id | test quotation for id |
| POST | `/api/sap/approve-order/` | JSON | `{ order_id, status }` | `{ success, msg }` |

### 5.4 HANA module (`/api/hana/`, live DB reads)

| Method | URL | Query/body | Response |
|---|---|---|---|
| GET | `/api/hana/product-stock/` | none | live stock list |
| GET | `/api/hana/so/` | `?card_code=` | open sales orders |
| GET | `/api/hana/product-so/` | `?item_code=` | open sales orders for item |
| GET | `/api/hana/open-parties/` | none | open parties |
| GET | `/api/hana/customer-details/` | `?card_code=` | customer + contacts |
| GET | `/api/hana/warehouse-details/` | `?whs_code=` | warehouse detail |
| GET | `/api/hana/salesperson-details/` | `?slp_code=` | salesperson detail |
| GET | `/api/hana/freight-masters/` | none | freight master |
| GET | `/api/hana/address/` | `?card_code=` | address list |
| GET | `/api/hana/state-chain/` | `?state_code=` | state chain |
| GET | `/api/hana/vendor-states/` | none | vendor state list |
| GET | `/api/hana/all-customers/` | none | all customers |
| GET | `/api/hana/next-doc-number/` | `?doc_type=` | next doc number |
| GET | `/api/hana/fg-items/` | none | FG items |
| GET | `/api/hana/batch-details/` | `?item_code=&whs_code=` | batch detail |
| GET | `/api/hana/inventory-details/` | `?item_code=` | inventory row list |
| GET | `/api/hana/item-price/` | `?item_code=&price_list=` | price list rows |
| GET | `/api/hana/series/` | `?finYear=&groupCode=` | series metadata |
| GET | `/api/hana/draft/verify` | `?refId=` | draft verify result |
| GET | `/api/hana/invoice-drafts/` | query filters | invoice draft rows |

### 5.5 Service-layer module (`/api/service-layer/`)

| Method | URL | Body | Response |
|---|---|---|---|
| POST | `/api/service-layer/invoice/` | invoice payload | SAP invoice result |
| POST | `/api/service-layer/draft/` | draft payload | SAP draft result |
| GET | `/api/service-layer/draft/` | `?draft_id=` | draft by id |
| POST | `/api/service-layer/draft-action/` | `{ draft_id, status }` + optional query `type` | action result |

### 5.6 Invoice module (`/api/invoice/`)

| Method | URL | Body / Query | Response |
|---|---|---|---|
| POST | `/api/invoice/log/create/` | log object | created invoice log |
| GET | `/api/invoice/all/` | `?status=` | invoice logs |
| POST | `/api/invoice/refLogs/` | ref log object | created invoice ref log |

### 5.7 SKU module (`/api/sku/`)

| Method | URL | Body | Response |
|---|---|---|---|
| POST | `/api/sku/upload/` | multipart upload | created/updated SKU |
| GET | `/api/sku/all/` | none | SKU list |
| GET | `/api/sku/pending/` | none | pending SKUs |
| GET | `/api/sku/<item_code>/` | none | one SKU |
| PUT | `/api/sku/<item_code>/` | partial/full SKU | updated SKU |
| PATCH | `/api/sku/<item_code>/` | partial SKU fields | updated SKU |
| DELETE | `/api/sku/<item_code>/` | none | delete status |

### 5.8 Legal module (`/api/legal/`)

| Method | URL | Body | Response |
|---|---|---|---|
| POST | `/api/legal/upload/` | multipart `label_file` | parse result + extracted labels |

---

## 6) DB ER-style text map (core tables)

### 6.1 Main domain relations (PostgreSQL)

```text
users_user (PK id)
  -> role_id --------> users_role(id)
  -> company_id -----> companies(id)
  -> main_group_id -> main_groups(id)
  -> state_id -------> states(id)
  -> M2M companies --------> users_user_main_groups(company_id, user_id)
  -> M2M states ----------- > users_user_states(user_id, state_id)
  -> assignments ---------> user_party_assignments(id, user_id, card_code, category)
  -> party-product maps ---> party_product_assignments(id, card_code, item_code, category, basic_rate)

orders_order (PK id)
  -> created_by -------------> users_user(id)
  -> status_id --------------> order_statuses(id)
  -> approved_by/rejected_by -> users_user(id)
  -> order_items -----------> order_items(id, order_id)
  -> orders_log -------------> orders_log(id, order_id, action, performed_by)
  -> order_item_schemes -----> order_item_schemes(id, order_item_id, scheme_id)
  -> order_rate_approvals --> order_rate_approvals(id, order_id, approver_id)
  -> order_item_approval_mapping --> order_item_approval_mapping(id, order_item_id, approver_id)
  -> notifications ---------> notifications(id, order_id, user_id)
  -> push_tokens -----------> push_tokens(id, user_id)
  -> templates.party --------> order_template(id, party_code)

sap_products
  PK/unique: item_code
  logical joins to orders by item_code and category

sap_parties
  PK/unique: card_code
  logical joins to users/orders by card_code and category

sap_party_addresses
  PK: id
  logical joins by card_code + address_name

sap_sync_logs
  -> scheduler/audit trails for sync and pull jobs

sales_quotation_logs
  -> stores quotation responses linked by order_id string

branches
  -> branch metadata used by routing logic

invoice_log
  -> created_by ----------> users_user(id)

invoice_ref_logs
  -> posted_by -----------> users_user(id)

sku
  -> item_code unique local catalog

labels
  -> parsed legal/uploaded document records

audit_log
  -> user_id ---------> users_user(id)
```

### 6.2 Cross-module join logic used in code (non-FK)

```text
orders_order.card_code   <--> sap_parties.card_code
orders_order.party_name  <--> sap_parties.card_name (via code/name normalization)
order_items.item_code    <--> sap_products.item_code
order_items.item_code    <--> party_product_assignments.item_code
users_user.id            <--> user_party_assignments.user_id
users_user.id            <--> party_product_assignments.assigned_by_id
orders_order.id          <--> sales_quotation_logs.order_id
orders_order.id          <--> notifications.order_id
orders_order.status_id    <--> order_statuses.id
```

---

## 7) SQL query references (inspect + update DB state)

### 7.1 Read queries

- All users with role + companies
```sql
SELECT u.id, u.username, u.email, r.name AS role, c.name AS company
FROM users_user u
LEFT JOIN users_role r ON r.id = u.role_id
LEFT JOIN companies c ON c.id = u.company_id
ORDER BY u.id DESC;
```

- Recent orders with status and creator
```sql
SELECT o.id, o.order_id, o.card_code, o.total_value, os.name AS status, u.username AS created_by
FROM orders_order o
LEFT JOIN order_statuses os ON os.id = o.status_id
LEFT JOIN users_user u ON u.id = o.created_by_id
ORDER BY o.created_at DESC
LIMIT 50;
```

- SAP sync log health
```sql
SELECT sync_type, status, COUNT(*) AS total
FROM sap_sync_logs
WHERE created_at >= NOW() - INTERVAL '7 days'
GROUP BY sync_type, status
ORDER BY sync_type, status;
```

### 7.2 Write queries

- Reset one order to draft (example maintenance)
```sql
UPDATE orders_order
SET status_id = (SELECT id FROM order_statuses WHERE name='Draft' LIMIT 1),
    updated_at = NOW()
WHERE id = :order_id;
```

- Insert custom party-product rate (temp admin fix)
```sql
INSERT INTO party_product_assignments (card_code, item_code, category, basic_rate, assigned_by_id, created_at)
VALUES (:card_code, :item_code, :category, :basic_rate, :user_id, NOW());
```

- Remove stale push token
```sql
DELETE FROM push_tokens
WHERE user_id = :user_id AND token = :token;
```

### 7.3 Django ORM equivalents

- list user orders
```python
from orders.models import Order
Order.objects.filter(created_by_id=user_id).select_related('status').order_by('-created_at')
```

- create order + items in one transaction
```python
from django.db import transaction
from orders.models import Order, OrderItem
with transaction.atomic():
    order = Order.objects.create(created_by=user, card_code='C001', status=status_obj)
    OrderItem.objects.create(order=order, item_code='ITM001', quantity=10)
```

---

## 8) File-by-file start point for new devs

Start here:
- [OMS-Backend/OMS/urls.py](OMS-Backend/OMS/urls.py)
- [OMS-Backend/users/urls.py](OMS-Backend/users/urls.py), [OMS-Backend/users/views.py](OMS-Backend/users/views.py), [OMS-Backend/users/models.py](OMS-Backend/users/models.py)
- [OMS-Backend/orders/urls.py](OMS-Backend/orders/urls.py), [OMS-Backend/orders/views.py](OMS-Backend/orders/views.py), [OMS-Backend/orders/models.py](OMS-Backend/orders/models.py)
- [OMS-Backend/sap_sync/urls.py](OMS-Backend/sap_sync/urls.py), [OMS-Backend/sap_sync/views.py](OMS-Backend/sap_sync/views.py), [OMS-Backend/sap_sync/models.py](OMS-Backend/sap_sync/models.py)
- [OMS-Backend/hana/urls.py](OMS-Backend/hana/urls.py), [OMS-Backend/hana/views.py](OMS-Backend/hana/views.py)
- [OMS-Backend/serviceLayer/views.py](OMS-Backend/serviceLayer/views.py), [OMS-Backend/serviceLayer/service.py](OMS-Backend/serviceLayer/service.py), [OMS-Backend/serviceLayer/urls.py](OMS-Backend/serviceLayer/urls.py)
- [OMS-Backend/invoice/urls.py](OMS-Backend/invoice/urls.py), [OMS-Backend/invoice/views.py](OMS-Backend/invoice/views.py)
- [OMS-Backend/SKU/views.py](OMS-Backend/SKU/views.py), [OMS-Backend/SKU/models.py](OMS-Backend/SKU/models.py)
- [OMS-Backend/legal/views.py](OMS-Backend/legal/views.py), [OMS-Backend/legal/models.py](OMS-Backend/legal/models.py)

---

## 9) Final checklist before handing to another developer

- [ ] `.env` and DB credentials verified.
- [ ] SAP/HANA endpoints reachable with correct TLS expectations.
- [ ] `DEBUG`, `SECRET_KEY`, CORS are production-safe.
- [ ] Permission classes standardized on protected modules.
- [ ] One sync run executed successfully (`/api/sap/sync/all/`).
- [ ] Query logs + order flow status changes verified.
- [ ] Basic admin + API smoke tests passed.
