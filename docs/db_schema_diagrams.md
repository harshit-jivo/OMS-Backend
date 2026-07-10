# DB Schema Diagrams (Mermaid)

This file documents the PostgreSQL schema used by OMS-Backend and the important runtime joins used across modules.

---

## 1) Core PostgreSQL ER diagram

```mermaid
erDiagram
    users_role {
        int id PK
        varchar name
        varchar display_name
        bool is_active
    }

    companies {
        int id PK
        varchar name
        bool is_active
    }

    main_groups {
        int id PK
        varchar name
        bool is_active
    }

    states {
        int id PK
        varchar name
        varchar code
        bool is_active
    }

    users_user {
        int id PK
        varchar username
        varchar name
        varchar phone
        int role_id FK
        int company_id FK
        int main_group_id FK
        int state_id FK
        int category_id FK
        bool is_superuser
        bool is_staff
        bool is_active
    }

    categories {
        int id PK
        varchar category
    }

    users_user_states {
        int id PK
        int user_id FK
        int state_id FK
    }

    scheme_product {
        int id PK
        varchar state_code
        int? category_id FK
        varchar item_code
        varchar scheme_name
        bool is_active
        bool is_scheme
    }

    party_product_assignments {
        int id PK
        int assigned_by_id FK
        varchar card_code
        varchar item_code
        varchar category
        decimal basic_rate
        bool is_active
        bool is_scheme
        int scheme_id FK
    }

    user_party_assignments {
        int id PK
        int user_id FK
        varchar card_code
        varchar category
        bool is_active
    }

    users_user ||--o{ users_user_states : has
    states ||--o{ users_user_states : links

    users_role ||--o{ users_user : has
    companies ||--o{ users_user : belongs_to
    main_groups ||--o{ users_user : belongs_to
    states ||--o{ users_user : primary_state
    categories ||--o{ users_user : category
    users_user ||--o{ users_user_states : user_states

    users_user ||--o{ users_user_states : assigned

    users_user ||--o{ user_party_assignments : assigned

    users_user ||--o{ party_product_assignments : assigns
    scheme_product ||--o{ party_product_assignments : optional_scheme
```

> Mermaid `erDiagram` does not support every join style; this is schema-level and does not cover all cross-module runtime links.

---

## 2) Orders domain (PostgreSQL)

```mermaid
erDiagram
    order_statuses {
        int id PK
        varchar code
        varchar name
        string color
        int sort_order
    }

    branches {
        int id PK
        int bpl_id
        varchar bpl_name
        varchar category
        bool is_active
    }

    parties {
        int id PK
        varchar card_code
        varchar card_name
        varchar state
        varchar main_group
        text address
    }

    dispatch_locations {
        int id PK
        varchar name
        varchar code
        varchar city
        bool is_active
    }

    party_addresses {
        int id PK
        varchar card_code
        varchar gst_number
        varchar address_type
        varchar address_name
        varchar category
    }

    product_details {
        int id PK
        varchar item_code
        varchar item_name
        varchar category
        varchar brand
        varchar variety
    }

    orders {
        int id PK
        varchar order_number
        int status_id FK
        int created_by_id FK
        varchar card_code
        int quotation_cancelled_by_id FK
        int approved_by_id FK
        int rejected_by_id FK
        decimal total_amount
        bool sap_created
        varchar sap_doc_number
        bool quotation_cancelled
        datetime created_at
    }

    order_items {
        int id PK
        int order_id FK
        varchar item_code
        varchar item_name
        decimal qty
        decimal pcs
        decimal boxes
        decimal ltrs
        decimal total
        decimal basic_price
        bool is_scheme_visible
        int scheme_id FK
    }

    orders_log {
        int id PK
        int order_id FK
        int action_id FK
        int performed_by_id FK
        text remarks
        datetime created_at
    }

    notifications {
        int id PK
        int user_id FK
        int order_id FK
        bool is_read
        varchar title
        text message
    }

    push_tokens {
        int id PK
        int user_id FK
        varchar token
        varchar platform
        bool is_active
    }

    order_flow_config {
        int id PK
        varchar flow_type
        bool rate_approval_enabled
        bool billing_enabled
        bool auditor_enabled
        int updated_by_id FK
    }

    party_order_flow_config {
        int id PK
        varchar card_code
        varchar category
        varchar flow_type
        bool rate_approval_enabled
        bool billing_enabled
        bool auditor_enabled
        int updated_by_id FK
    }

    order_template {
        int id PK
        int user_id FK
        int order_id FK
        varchar sub_group
    }

    order_item_schemes {
        int id PK
        int order_item_id FK
        int scheme_id FK
        decimal qty_scheme
    }

    staff_product_prices {
        int id PK
        int product_id FK
        decimal rate
        bool is_active
    }

    rate_approver_rules {
        int id PK
        int approver_id FK
        varchar category
        varchar variety
        decimal max_rate_limit
    }

    order_rate_approvals {
        int id PK
        int order_id FK
        int approver_id FK
        varchar status
    }

    order_item_approval_mapping {
        int id PK
        int order_id FK
        int order_item_id FK
        int approver_id FK
        varchar status
    }

    orders ||--o{ order_items : contains
    order_statuses ||--o{ orders : status
    users_user ||--o{ orders : created_by
    users_user ||--o{ orders : approved_by
    users_user ||--o{ orders : rejected_by
    users_user ||--o{ orders : cancelled_by

    orders ||--o{ orders_log : tracks
    order_statuses ||--o{ orders_log : action
    users_user ||--o{ orders_log : logs_by

    users_user ||--o{ notifications : receives
    orders ||--o{ notifications : relates_to

    users_user ||--o{ push_tokens : owns

    orders ||--o{ order_template : referenced_in
    users_user ||--o{ order_template : created_template

    order_items ||--o{ order_item_schemes : has
    scheme_product ||--o{ order_item_schemes : applies

    orders ||--o{ order_rate_approvals : approval_workflow
    order_rate_approvals }o--|| users_user : assigned_approver

    order_items ||--o{ order_item_approval_mapping : approver_decisions
    order_item_approval_mapping }o--|| users_user : mapped_approver
    orders ||--o{ order_item_approval_mapping : per_order

    users_user ||--o{ order_flow_config : updates
    branches ||--o{ orders : dispatch_ref
    branches ||--o{ order_items : used_by_location

    users_user ||--o{ staff_product_prices : sets
    order_items ||--o{ staff_product_prices : linked_product

    users_user ||--o{ rate_approver_rules : owns
```

> `product_details`, `parties`, `dispatch_locations`, and `party_addresses` are read through orders flows; they are not all directly FK-linked in the current model due legacy/import layout.

---

## 3) SAP Sync + legal/ invoice / SKU tables

```mermaid
erDiagram
    sap_products {
        int id PK
        varchar item_code
        varchar item_name
        varchar category
        varchar variety
        varchar type
        varchar is_deleted
        bool is_active
        datetime synced_at
    }

    sap_parties {
        int id PK
        varchar card_code
        varchar card_name
        varchar card_type
        varchar category
        varchar state
        varchar main_group
        datetime synced_at
    }

    sap_party_addresses {
        int id PK
        varchar card_code
        varchar address_name
        varchar address_type
        varchar gst_number
        varchar state
        varchar category
        datetime synced_at
    }

    sap_sync_logs {
        int id PK
        varchar sync_type
        varchar status
        int records_processed
        int records_created
        int records_updated
        int records_failed
        datetime started_at
        datetime completed_at
        varchar triggered_by
    }

    sap_sync_schedules {
        int id PK
        varchar name
        varchar sync_type
        varchar frequency
        int custom_interval_minutes
        int hour
        bool is_active
        datetime last_run
        datetime next_run
    }

    sales_quotation_logs {
        int id PK
        varchar order_id
        int sap_doc_entry
        int sap_doc_num
        varchar status
        datetime started_at
        datetime completed_at
    }

    branches {
        int id PK
        int bpl_id
        varchar bpl_name
        varchar category
        bool is_active
    }

    invoice_log {
        int id PK
        varchar so_number
        varchar card_name
        decimal total_amount
        varchar status
        int approved_by_id FK
        int rejected_by_id FK
        int created_by_id FK
        datetime created_at
    }

    invoice_ref_logs {
        int id PK
        varchar ref_id
        varchar card_name
        varchar so_number
        varchar status
        int posted_by_id FK
        datetime posted_at
    }

    sku {
        int id PK
        varchar item_code
        varchar item_name
        varchar item_image
        datetime uploaded_at
    }

    labels {
        int id PK
        varchar label_file
        jsonb parameter_json
        datetime uploaded_at
    }

    audit_log {
        int id PK
        int user_id FK
        varchar username
        varchar page
        varchar action
        varchar record
        varchar field
        datetime created_at
    }

    sap_products ||--o{ sales_quotation_logs : push_reference
    sap_parties ||--o{ sales_quotation_logs : quote_for_party
    sap_products ||--o{ sku : optional_image_sync

    users_user ||--o{ invoice_log : created_or_reviewed
    users_user ||--o{ invoice_ref_logs : posted_by

    users_user ||--o{ audit_log : actor
```

> Note: `invoice_log` / `invoice_ref_logs` are lightweight operational logs, not part of the core order workflow.

---

## 4) Cross-module/runtime joins (important for onboarding)

The following are relationships used in code logic but **not strict Django FK constraints**:

- `orders.card_code` -> `sap_parties.card_code` and also many `sap_parties` rows can exist by `(card_code, category)`.
- `orders.card_name` -> `sap_parties.card_name` (normalization fallback when card code lookup is partial).
- `order_items.item_code` -> `sap_products.item_code`.
- `order_items.item_code` + `orders.order_type`/`card_code` -> `party_product_assignments` for pricing (rate check).
- `sales_quotation_logs.order_id` (string) -> `orders.id` (int) for response lookup.
- `party_product_assignments.card_code` + `item_code` -> `sku.item_code` for completed image check in `/api/sku/pending/`.
- `orders.status_id` + `party_order_flow_config` / `order_flow_config` decide approval pipeline behaviour at runtime.

---

## 5) Rendering tips (Mermaid)

- VS Code: enable Mermaid preview extension and open this file to visualize.
- Docs portals: GitHub markdown preview supports Mermaid if enabled in repo settings.
- If rendering fails, copy the diagram blocks into https://mermaid.live for quick visual validation.

---

## 6) Useful references

- [users-models.py](OMS-Backend/users/models.py)
- [orders-models.py](OMS-Backend/orders/models.py)
- [sap_sync/models.py](OMS-Backend/sap_sync/models.py)
- [invoice/models.py](OMS-Backend/invoice/models.py)
- [SKU/models.py](OMS-Backend/SKU/models.py)
- [legal/models.py](OMS-Backend/legal/models.py)
- [audit/models.py](OMS-Backend/audit/models.py)
