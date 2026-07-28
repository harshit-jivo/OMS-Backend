# OMS Backend — Project Overview

## 1. What this project is

OMS stands for **Order Management System**. This backend is the business and integration layer of an internal order-processing platform. Its main purpose is to let authorized employees create and manage customer or staff orders in a controlled workflow before sending approved business documents to SAP.

It is not an online shopping backend. It is an enterprise operations system built around customers (called **parties**), products, party-specific prices, promotional schemes, approval roles, stock availability, SAP sales quotations, and invoices.

The backend is a Django REST API. The frontend calls its APIs; PostgreSQL stores OMS data; SAP/HANA remains the source or destination for important ERP data and documents.

## 2. The business problem it solves

An organization using SAP can still have a difficult process before an order is ready for SAP. Salespeople need the correct customer, address, product, price, scheme, warehouse, and stock information. Special prices may require approval. Billing and auditing teams may need to review an order. Managers need to know where an order is blocked. Manual entry in several systems creates delays and mistakes.

The likely process before OMS was fragmented across calls, messages, spreadsheets, paper, and direct ERP entry. That creates common problems:

- A salesperson may select a customer or product they are not responsible for.
- The wrong customer-specific price or promotional scheme may be used.
- Discounted or unusual rates may bypass the right approver.
- Billing and audit teams may receive incomplete information.
- The same data may be typed once outside SAP and again inside SAP.
- Stock may be checked too late.
- Nobody has one clear view of an order's current stage or complete history.
- SAP synchronization or document failures are hard to trace.

OMS centralizes and controls that work.

## 3. Before and after OMS

### Before

1. Customer, product, price, and stock data are gathered from different people or systems.
2. Order details are communicated manually.
3. Approvals depend on messages and individual follow-up.
4. Approved details are re-entered in SAP.
5. Status and accountability are difficult to see.
6. Reporting requires collecting information manually.

### After

1. SAP master data is synchronized into OMS and live SAP/HANA information can be queried when required.
2. Each user sees data allowed by their role, state, category, subgroup, and party assignments.
3. An order is created from structured customer, address, product, quantity, price, tax, and scheme information.
4. OMS decides whether rate approval is required and routes the order through configured stages.
5. Approvers, billing users, and auditors act on the same order record.
6. Notifications and dashboards expose pending work.
7. An approved order can become an SAP sales quotation, with request, response, and document numbers logged.
8. Every important order action and many administrative changes leave an audit trail.

## 4. Business impact

The intended impact is:

- **Faster order processing:** less manual coordination and duplicate entry.
- **Fewer commercial errors:** party-specific products, prices, taxes, and schemes are applied consistently.
- **Stronger approval control:** exceptions are sent to the appropriate rate approver, billing user, or auditor.
- **Better accountability:** actions record who did what and when.
- **Better visibility:** dashboards, status tracking, logs, and notifications reveal pending and completed work.
- **Closer SAP integration:** OMS acts as a controlled operational layer around SAP instead of replacing it.
- **Safer data access:** assignments and organizational attributes restrict which customers and product groups a user handles.
- **Operational scalability:** configurable flows and automated synchronization reduce dependence on informal knowledge.

## 5. Main people and roles

Roles are stored in the database rather than being completely hard-coded. The implementation specifically recognizes roles such as:

- **Order creator / ASM-side user:** prepares party or staff orders.
- **Rate approver:** reviews items whose submitted rates meet configured exception conditions.
- **Billing user:** performs the commercial/billing stage and can use a separate billing-originated flow.
- **Auditor:** performs the final review stage when enabled.
- **Admin:** manages users, assignments, permissions, configuration, and broad reporting.

A user can be scoped by company, main group, state, product category, subgroup, assigned parties, and extra page permissions. A party can be assigned to one or more users by category. Products and negotiated basic rates are separately assigned to parties.

## 6. The core order journey

The exact stages are configurable, but the general journey is:

```text
SAP master data / administrator assignments
                    |
                    v
          User selects an allowed party
                    |
                    v
        Products, prices, schemes, addresses
                    |
                    v
          Create or save a draft order
                    |
                    v
       Rate exception evaluation (if enabled)
                    |
         +----------+----------+
         |                     |
         v                     v
   Rate Approval          Skip this stage
         |                     |
         +----------+----------+
                    v
             Billing stage
                    v
             Auditor stage
                    v
               Completed
                    v
          SAP sales quotation / logs
```

Disabled stages are skipped. A party/category-specific configuration can override the global flow. Free-of-cost orders receive special flow handling. A billing user can also start a billing-oriented flow, which begins later than the standard ASM-oriented flow.

At each transition the backend can update the status, write an order log, mark old notifications as read, create new notifications, and determine the next responsible users.

### Drafts and updates

An order may be saved as **Draft** and later edited or deleted. Submitting or updating a draft recalculates its path. If it returns to rate approval, prior approval decisions can be reset so an old decision is not incorrectly reused.

### Rate approval

The flow configuration contains rate conditions such as a submitted basic price being greater than a comparison or market price. Rate approver rules map product categories and subgroups to approver users. OMS creates per-approver approval records and maps affected order items to those approvers. This allows different parts of one order to be reviewed by the relevant people.

### Approval and rejection

Approvals move the order to its next enabled stage. Rejection requires a reason and records the rejecting user and time. The order log provides a chronological status history. Billing, auditor, and rate-approver decisions are also used in role-specific dashboards and tracking views.

### SAP quotation

When the business flow permits it, OMS maps an order into SAP's Sales Quotation format. It chooses the relevant SAP company database and warehouse based on order/category configuration, authenticates with SAP Service Layer, posts the quotation, and stores the request, response, success/failure state, SAP document entry, and SAP document number. A manager can also cancel a quotation in SAP and mirror that state in OMS.

## 7. Important business data

### Order header

An order records its order number, party code/name, billing and shipping addresses, dispatch branch, company, PO number, delivery date, remarks, total amount, type, creator, status, approval/rejection information, and SAP result. Orders can be normal party orders or staff orders; staff orders can carry an employee ID. Free-of-cost orders are explicitly identified.

### Order items

Each item records product identity and classification, quantities in several units, price-list basic price, submitted basic price, total, tax, promotional scheme, and scheme quantity.

### Parties, products, and rates

- A **party** is an SAP customer/business partner.
- A **party address** contains billing or shipping details and GST information.
- A **product** contains SAP item details, grouping, units, price, tax, active state, and stock-related data.
- A **party-product assignment** says that a party may buy a product in a category and stores that party's basic rate and optional scheme.
- A **user-party assignment** says which user may work with which party/category.

### Promotional schemes

Schemes can be assigned by item and state, and an order item may hold one or multiple scheme entries. The code includes special handling for combo products and Punjab-specific scheme-quantity behavior. These rules exist to translate real commercial promotions into the quantities expected by OMS and SAP.

## 8. Major modules

### `users` — identity and business access

Provides JWT login, profiles, users, roles, companies, states, main groups, categories, page permissions, party assignments, party-product assignments, bulk assignment, and customer-specific rates. This module answers: **who may use the system, and what business data may they work with?**

### `orders` — the main business engine

Provides party/product selection, order creation and editing, drafts, configurable workflows, approvals and rejections, rate-approver rules, schemes, stock checks, saved order templates, status tracking, order history, dashboards, notifications, push tokens, quotation status, and quotation cancellation. This is the center of the project.

### `sap_sync` — local SAP master-data mirror and quotation bridge

Synchronizes products, parties, addresses, and branches from the SAP-side database into PostgreSQL. Sync may be manual or scheduled. Every run has a type, trigger, counts, timestamps, status, and error details. The same module maps OMS orders to SAP sales quotations and logs the complete result.

### `hana` — live ERP queries

Queries SAP HANA for current operational information such as product stock, open sales orders, customer details, warehouses, salespeople, addresses, freight masters, finished goods, batches, inventory, item prices, numbering series, quotation status, and invoice drafts. In simple terms, `sap_sync` maintains convenient local copies while `hana` retrieves information that should be live.

### `serviceLayer` — SAP document operations

Manages authenticated SAP Service Layer sessions. It creates invoice documents or drafts, reads drafts, finds SAP approval requests, and approves or rejects a draft using an SAP approver account.

### `invoice` — OMS invoice tracking

Stores invoice approval records and payloads with pending, approved, rejected, or error states. It also stores reference logs showing which user posted an invoice-related reference, its status, and any error. SAP document creation itself is handled by `serviceLayer`.

### `SKU` — product image catalogue

Uploads, lists, updates, and deletes SKU records containing item code, item name, and image. It also exposes a pending list, supporting product-content preparation or review.

### `legal` — food-label extraction

Accepts a product-label PDF, converts its first page to an image, sends it to an AI model with an FSSAI-focused extraction prompt, and stores structured JSON. The extracted fields include food name, ingredients, nutrition, manufacturer, FSSAI details, date information, MRP/cost block, barcode, trademark, and compliance declarations. This assists label review; it should not be treated as a substitute for final legal verification.

### `audit` — administrative accountability

Middleware and signals record changes made through recognized administration pages. Audit rows capture the user, page, action, record, field, old value, new value, and time. The username is copied into the log so history survives deletion of the user account.

## 9. How information moves through the system

There are three main data directions:

1. **SAP to OMS:** customers, products, addresses, branches, and related master data are synchronized into PostgreSQL.
2. **OMS internal processing:** users create orders; OMS applies assignments, rates, schemes, workflow configuration, approvals, logs, dashboards, and notifications.
3. **OMS to SAP:** approved order data becomes a sales quotation; invoice or draft payloads can also be sent through SAP Service Layer.

For data such as current stock and open sales orders, OMS can query HANA directly instead of relying only on the synchronized copy.

## 10. Dashboards, tracking, and notifications

The backend offers general and role-aware dashboard endpoints. They calculate counts and charts for pending, approved, rejected, completed, billing, auditor, and rate-approval work. Data is scoped to the user's allowed organization and assignments.

Order tracking combines the current status with historical decisions. Order logs show the sequence of actions. In-app notifications identify new work, and stored mobile push tokens support device notifications. Saved order templates let users reuse earlier party/order selections.

## 11. Technical shape, in plain language

- **Django + Django REST Framework:** receives HTTP requests and runs the business rules.
- **JWT authentication:** login returns an access token used on protected requests.
- **PostgreSQL:** stores OMS users, assignments, orders, approvals, synchronized data, and logs.
- **SAP source connection:** reads ERP master data, configured here through a SQL Server-compatible connection.
- **SAP HANA connection:** runs live operational queries.
- **SAP Service Layer:** creates and manages ERP documents over HTTP.
- **APScheduler:** supports scheduled SAP synchronization.
- **File/media storage:** stores SKU images and uploaded label PDFs.
- **AI integration:** provides label-field extraction; order summary support exists but is currently minimal/optional.

The backend exposes grouped APIs under `/api/auth/`, `/api/orders/`, `/api/sap/`, `/api/hana/`, `/api/sku/`, `/api/service-layer/`, `/api/invoice/`, and `/api/legal/`.

## 12. What OMS owns and what SAP owns

This distinction is important:

- **OMS owns the preparation process:** access, assignments, customer-specific rates, drafts, configurable approvals, internal statuses, notifications, dashboards, and traceability.
- **SAP owns the ERP result:** authoritative business partners/items, live inventory and sales information, sales quotation documents, drafts, approvals, and invoices.

OMS is therefore a workflow and usability layer around SAP, not an ERP replacement.

## 13. Example from start to finish

Suppose an ASM needs to place an oil order for a distributor:

1. The ASM logs in and receives a JWT token.
2. OMS only shows parties assigned to that ASM and category.
3. The ASM selects the distributor; OMS supplies its addresses and assigned products.
4. OMS supplies the distributor-specific basic rate and eligible schemes.
5. The ASM enters quantities, PO data, delivery date, and dispatch location.
6. The order is saved as a draft or submitted.
7. OMS checks configured rate conditions. Exceptional item rates go to mapped rate approvers.
8. After required rate decisions, the order moves to Billing and then Auditor Approval, skipping any globally or party-specifically disabled stage.
9. Each action creates history and alerts the next responsible users.
10. Stock can be checked against live ERP data.
11. The completed order is mapped and posted as an SAP sales quotation.
12. OMS records the SAP document number or a detailed failure log.
13. Managers can see the outcome in order lists, tracking screens, and dashboards.

## 14. Current boundaries and important observations

The repository shows an actively evolving business system, so some behavior depends on database setup and environment configuration rather than code alone. In particular, role names, order-status rows, party assignments, workflow configuration, SAP credentials, company databases, warehouse codes, and rate rules must be correctly maintained.

Some endpoints currently allow unauthenticated access or rely on broad global settings, and development-oriented configuration is present. The legal extraction service also contains environment-specific setup. Those are deployment/security concerns to address before treating the system as production-hardened; they do not change its business purpose.

The legal AI output is an extraction aid, not a legal decision. SAP remains the authoritative source for ERP documents and live inventory. Synchronization failures, stale master data, missing assignments, or incorrect workflow configuration can affect what users see and how orders route.

## 15. One-sentence summary

**OMS turns a scattered, manually coordinated pre-SAP ordering process into a structured, permission-controlled, price-aware, approval-driven, traceable workflow that connects employees, customers, products, stock, quotations, invoices, and SAP.**

