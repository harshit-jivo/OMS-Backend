# OMS-Backend `orders` Module - Beginner Friendly Deep Notes (Express + DB lens)

This note focuses only on the `orders` module first, as requested.

## 1) Module architecture in plain language

If you already know MERN/Express, think of this mapping:

- `orders/models.py` = DB schema/models (similar to Prisma/Sequelize schema definitions)
- `orders/serializers.py` = request/response shape + validation (similar to `zod`/`Joi` DTOs)
- `orders/views.py` = business logic + route handlers (controller/service combined)
- `orders/urls.py` = express router (`router.get('/create', ...)`, `router.post('/update', ...)`)
- migrations = DB change history (like Prisma migrations / SQL migration files)
- `orders/apps.py` = app bootstrap registration (similar to enabling an express module)
- `orders/scheme_rules.py` = pure business rules (helper/service functions)
- `orders/ai_service.py` = helper service, called by `/api/orders/api/ai-order-summary/`

The module is mounted in project routing from:

- [OMS-Backend/OMS/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\OMS\urls.py)

and includes every `orders` endpoint under:

- `/api/orders/...`

## 2) Files in this module

- [orders/models.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\models.py)
- [orders/serializers.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\serializers.py)
- [orders/views.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\views.py)
- [orders/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\urls.py)
- [orders/scheme_rules.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\scheme_rules.py)
- [orders/ai_service.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\ai_service.py)
- [orders/migrations/*.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\migrations)

## 3) Domain tables (`db_table`) used by `orders`

From models and migrations, these are the main physical tables:

- `orders`
- `order_items`
- `order_statuses`
- `orders_log`
- `order_flow_config`
- `order_template`
- `order_item_schemes`
- `notifications`
- `push_tokens`
- `staff_product_prices`
- `categories`
- `dispatch_locations`
- `parties`
- `party_addresses`
- `product_details`
- `branches` (read through model `Branches`, not managed by this module)

## 4) Important workflows and why they matter

### Core business lifecycle

- `orders` has a state machine using `order_statuses`.
- Workflow metadata is stored in `order_flow_config` and can alter next-stage behavior per deployment/runtime.
- Audit of status changes is in `orders_log`.

Important status seeds (from migration) are:

- `Order Created` (`code: CREATED`, id 1)
- `Rate Approval` (`code: RATE_APPROVAL`, id 2)
- `Billing` (`code: BILLING`, id 3)
- `Need Approval` (`code: NEED_APPROVAL`, id 4)
- `Billing Pending` (`code: BILLING_PENDING`, id 5)
- `Approved` (`code: APPROVED`, id 6)
- `Rejected` (`code: REJECTED`, id 7)
- `Billing Rejected` (`code: BILLING_REJECTED`, id 8)
- `Completed` (`code: COMPLETED`, id 9)
- `Auditor Approval` (`code: AUDITOR_APPROVAL`, id 10)

Source seeds:
- [orders/migrations/0002_populate_order_statuses.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\migrations\0002_populate_order_statuses.py)

### Where config changes workflow behavior

- `OrderFlowConfig` stores toggles:
  - `rate_approval_enabled`
  - `billing_enabled`
  - `auditor_enabled`
  - `rate_conditions` (JSON rules like `BASIC_GT_MARKET`, etc.)
  - `flow_type` (`ASM`, `BILLING`) (from migrations `0034`, `0035`)
- Defaults for ASM and Billing flows are seeded by migrations.
- See:
  - [orders/models.py#OrderFlowConfig](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\models.py)
  - [orders/migrations/0033_orderflowconfig.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\migrations\0033_orderflowconfig.py)
  - [orders/migrations/0034_orderflowconfig_flow_type.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\migrations\0034_orderflowconfig_flow_type.py)

### Flow summary (what happens on create/update)

- Create order -> status picked by `_get_initial_flow_status()` based on:
  - enabled flow steps
  - whether any line sells below the party's agreed rate on
    `party_product_assignments` (`_get_rate_approval_reason()`); the old
    price-diff condition codes (basic vs market price) are only a fallback
    when no agreed-rate verdict is passed in
- Update order -> can recalc totals and possibly re-evaluate next status (update endpoint logic)
- `update-status` endpoint -> explicit status transitions with logging + notifications
- `approve` and `reject` are legacy/simple shortcuts for created orders, but current flow has richer `update-status`.

Relevant handlers:
- [orders/views.py#CreateOrderView](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\views.py)
- [orders/views.py#UpdateOrderView](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\views.py)
- [orders/views.py#UpdateOrderStatusView](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\views.py)
- [orders/views.py#ApproveOrderView](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\views.py)
- [orders/views.py#RejectOrderView](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\views.py)

## 5) API endpoints by route + operation

From:
- [orders/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\urls.py)

All endpoints are under `/api/orders/`.

- `GET /parties/` -> party lookup for order UI
- `GET /dispatches/` -> dispatch location list
- `GET /addresses/?card_code=&type=` -> bill/ship addresses
- `GET /product-filters/` -> product filters (category/brand/variety/type)
- `GET /products/` -> product list for cards and modules
- `POST /create/` -> create new order
- `PUT /<order_id>/update/` -> update existing order, items, totals
- `GET /list/` -> general order list query
- `POST /<order_id>/approve/` -> simple approve flow
- `POST /<order_id>/reject/` -> simple reject flow
- `GET /party-products/<card_code>/` -> party specific products and active staff rates
- `GET /schemes/` -> scheme catalog
- `GET /status/` -> status master
- `GET/POST /flow-config/?flow_type=ASM|BILLING` -> read/update flow config
- `GET /branch/` -> branch list
- `POST /<order_id>/update-status/` -> manual status transition with log
- `GET /dashboard/`, `GET /dashboard/charts/` -> dashboard KPIs/charts
- `GET /dashboardW/`, `GET /dashboardW/charts/` -> alternate dashboard
- `GET /status-tracking/` -> status summary/filter tracking
- `GET /<order_id>/orderlogs/` -> order logs
- `GET /orderdetails/<order_id>/` and `GET /orderdetailsbyid/<order_id>/` -> full order view
- `GET /ordersbyuser/<user_id>/` -> orders by specific user
- `POST /create-scheme/` -> add new scheme
- `GET /templates/parties/` -> template parties for current user
- `GET /templates/orders/?card_code=` -> template order list
- `POST /api/ai-order-summary/` -> AI summary helper
- `GET /notifications/` and `POST /notifications/`, `PATCH /notifications/<id>/` -> notification actions
- `POST /push-token/` -> save expo push token
- `GET /stock-check/` -> live stock check (SAP connection query path)
- `GET/POST /staff-products/` -> staff price mapping
- `GET /?user=<id>` inside `UserPartyView` is an internal endpoint class (module file includes this view near end)

## 6) Queries used (ORM-to-SQL mindset)

This section lists the important query patterns implemented in this module.

### a) GET list views

- Basic list/filter:

```sql
SELECT * FROM orders
WHERE status_id = :status_id
AND created_by = :user_id
ORDER BY created_at DESC;
```

- Role-based list uses `orders_log` and assigned tables (user/party/team scopes) before selecting `orders`.

### b) Create order

Main writes:

```sql
INSERT INTO orders (...) VALUES (... initial fields, status_id, created_by, total_amount, created_at...);
```

```sql
INSERT INTO order_items (order_id, item_code, qty, basic_price, market_price, total, scheme_id, qty_scheme, is_scheme_visible, ...) 
VALUES (...), (...), ...;
```

```sql
INSERT INTO order_item_schemes (order_item_id, scheme_id, qty_scheme) VALUES (...);
```

```sql
INSERT INTO orders_log (order_id, action_id, performed_by, remarks, created_at) VALUES (...);
```

### c) Update order

- Recreate item rows:

```sql
DELETE FROM order_items WHERE order_id = :id;
-- plus related item-scheme cleanup
INSERT INTO order_items ...;
INSERT INTO order_item_schemes ...;
UPDATE orders SET total_amount = :sum, status_id = :new_status, updated_at = NOW() WHERE id = :id;
INSERT INTO orders_log ...;
```

### d) Status transition endpoint

```sql
SELECT * FROM orders WHERE id = :order_id;
SELECT * FROM order_statuses WHERE id = :status_id;
UPDATE orders SET status_id = :status_id, updated_at = NOW(), ... WHERE id = :order_id;
INSERT INTO orders_log (order_id, action_id, performed_by, remarks) VALUES (:order_id, :status_id, :user_id, :reason);
```

### e) Notification and push

- Notification writes:

```sql
INSERT INTO notifications (user_id, order_id, message, is_read, created_at)
VALUES (...);
```

- Push token management:

```sql
INSERT INTO push_tokens (user_id, token, platform, is_active, created_at, updated_at)
VALUES ... ON CONFLICT (token) DO UPDATE SET user_id = EXCLUDED.user_id, ...;
```

## 7) Role-based access behavior (important for onboarding)

The code uses role names from user profile:

- `admin` -> usually broad visibility
- `manager` -> creator-specific view
- `approver`, `billing`, `auditor` -> role-specific queue filtering by status/logs and party scope
- `staff` -> simplified path for staff order type

Role model source:
- [users/models.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\users\models.py)

## 8) Data read dependencies from other apps

This module does not keep all master data itself; it reads references from:

- `users` module
  - `scheme_product`, `party_product_assignments`, `user_party_assignments`, `User`
- `sap_sync` module
  - `sap_products`, `sap_parties`, `sap_party_addresses`, `sales_quotation_logs`

Sources:
- [users/models.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\users\models.py)
- [sap_sync/models.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\sap_sync\models.py)

## 9) Express mental model example

For a MERN dev, this can be thought of as:

- `CreateOrderView.post` -> `ordersController.createOrder(req, res)`
- `UpdateOrderView.put` -> `ordersController.updateOrder(req, res)`
- `UpdateOrderStatusView.post` -> `ordersController.updateOrderStatus(req, res)`
- `OrderListView.get` + query params -> `ordersController.listOrders(req, res)` with `req.query` filters
- ORM model usage -> Prisma models (`Order`, `OrderItem`, etc.)

Equivalent structure in Express:

- `router.use('/api/orders', ordersRouter)`
- `ordersRouter.post('/create', auth(), createOrder)`
- `ordersRouter.get('/list', authOptional(), listOrders)`
- etc.

## 10) Where to check DB writes when debugging

- If order creation is broken:
  - verify row in `orders`
  - verify child rows in `order_items`
  - verify audit row in `orders_log`
- If status machine not moving:
  - check `order_flow_config`
  - check transition row in `orders_log`
  - check `order_statuses.code/name`
- If approvals/queue empty:
  - check `user.role`
  - check `_get_base_orders` user-scope path
  - check `orders_log` for current pending actor
- If stock/check page fails:
  - check SAP integration path with `SAPConnection` inside stock check function

## 11) Quick "how to read this module next"

1. Open [orders/views.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\views.py) and trace only:
   - `_get_base_orders`
   - `CreateOrderView.post`
   - `UpdateOrderView.put`
   - `UpdateOrderStatusView.post`
2. Open [orders/models.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\models.py) to map each field you see changed in API calls.
3. Open migrations for current status/config baseline:
   - [orders/migrations/0002_populate_order_statuses.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\migrations\0002_populate_order_statuses.py)
   - [orders/migrations/0033_orderflowconfig.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\migrations\0033_orderflowconfig.py)
   - [orders/migrations/0034_orderflowconfig_flow_type.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\migrations\0034_orderflowconfig_flow_type.py)

## 12) Practical note for a new developer

This module is the central workflow engine.
Start here first, then move to:
- `sap_sync` for master data ingestion and sync logs,
- `users` for role/scoping,
- `serviceLayer` / `sap_sync/services` for SAP push side effects.

After this module docs are done, I can now write the next module doc in the same format (e.g., `sap_sync`, `users`, `hana`).
