# OMS-Backend Project Architecture (Full Project View) + Node/Express Mapping

This document connects all module docs into one end-to-end picture for a MERN/Next.js developer.

## 1) Project-level entry points

- [OMS/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\OMS\urls.py)
  - Auth APIs: `/api/auth/`
  - Orders: `/api/orders/`
  - SAP sync: `/api/sap/`
  - Live HANA reads: `/api/hana/`
  - SKU: `/api/sku/`
  - Service Layer: `/api/service-layer/`
  - Invoice: `/api/invoice/`
  - Legal: `/api/legal/`
- Middleware chain in [OMS/settings.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\OMS\settings.py) includes:
  - `audit.middleware.AuditMiddleware` (admin change tracking side-effect).
- Auth globally uses JWT:
  - [OMS/settings.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\OMS\settings.py) (`DEFAULT_AUTHENTICATION_CLASSES`).

## 2) Core architecture style (at a glance)

- Django project acts as one API server with app-level route namespaces (`include('module.urls')`), equivalent to a single Express app mounting multiple routers.
- Most business logic is inside `APIView` or helper functions per module.
- Data storage is PostgreSQL via Django ORM models.
- External integrations:
  - SAP DB read/sync via SQL connections and direct SQL in `sap_sync`.
  - HANA live reads in `hana`.
  - SAP Service Layer via `requests` sessions in `serviceLayer`.
  - Gemini AI call from `legal`.

## 3) Database footprint by module

- `users` -> user identity, permissions, party/product assignments, schemes, config.
- `orders` -> core order headers, items, logs, notifications, templates, workflows.
- `sap_sync` -> synced SAP master records + sync/audit logs.
- `hana` -> live operational reads mostly through stored-query style SQL wrappers.
- `SKU` -> product image catalog (`sku` table).
- `serviceLayer` -> no core tables for flow logic (acts as passthrough to SAP API).
- `invoice` -> invoice/audit trail references (`oms_invoices`, `invoice_references` table family).
- `legal` -> label file + extracted JSON (`labels` table).
- `audit` -> immutable `audit_log` table for admin change tracking.

## 4) End-to-end flow in plain language

1. User logs in / gets JWT.
2. User calls order endpoints in `orders`:
   - lookup parties/products/assignments from `users/sap_sync`.
3. User drafts/submits order.
4. OMS evaluates configured workflow (`users` assignments + `orders` flow config + rate approval rules).
5. Approvers/Billing/Auditors act in separate endpoints and actions.
6. On completion and when allowed, `orders` maps OMS order to SAP payload and posts via `serviceLayer`/`sap_sync`.
7. Response/result is written back to OMS logs (`orders` + `sap_sync.sales_quotation_logs` equivalent paths).
8. `audit` captures who changed what on admin routes automatically.

## 5) Module-to-Express mapping cheatsheet

- `OMS/urls.py` -> `app.use('/api/...', route)`
- App `urls.py` per module -> `router.js` file.
- DRF `APIView`/`ViewSet` -> Express controller functions.
- `serializers.py` -> schema validation middleware (body shape + allowed fields).
- `models.py` -> Mongoose/Prisma schemas.
- `permissions_classes` -> Express auth/authorization middleware.
- `services.py` helpers -> service layer functions.
- `signals.py` + `middleware.py` in `audit` -> cross-cutting middleware/hook layer.

## 6) Module docs index (read in this order first)

- [overview.md](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\docs\overview.md)
- [users-module-workflow.md](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\docs\users-module-workflow.md)
- [orders-module-workflow.md](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\docs\orders-module-workflow.md)
- [sap_sync-module-workflow.md](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\docs\sap_sync-module-workflow.md)
- [hana-module-workflow.md](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\docs\hana-module-workflow.md)
- [serviceLayer-module-workflow.md](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\docs\serviceLayer-module-workflow.md)
- [invoice-module-workflow.md](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\docs\invoice-module-workflow.md)
- [SKU-module-workflow.md](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\docs\SKU-module-workflow.md)
- [legal-module-workflow.md](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\docs\legal-module-workflow.md)
- [audit-module-workflow.md](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\docs\audit-module-workflow.md)

## 7) What this means for a beginner in MERN/Next.js

- You do not need to re-learn business flow from scratch; focus on:
  - one API entry (`OMS/urls.py`),
  - one serializer/validation layer,
  - one service layer for external integrations,
  - one ORM mapping per module.
- For `orders` onboarding, the hardest part is not syntax — it is workflow branching:
  - draft/saved state,
  - conditional approver stages,
  - role-based transitions,
  - SAP posting side effects.

## 8) Suggested learning order

1. `users` (who can do what)
2. `orders` (main lifecycle)
3. `sap_sync` + `hana` (where reference data comes from)
4. `serviceLayer` + `invoice` (integration-to-SAP path)
5. `audit` (cross-cutting operational safety)
6. `SKU` + `legal` (supporting admin workflows)

