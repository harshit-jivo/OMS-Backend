# OMS-Backend `users` Module - Beginner Friendly Deep Notes (Express + DB lens)

This is the second module doc (after `orders`).  
It documents how `users` feeds login, role assignment, party scope, and product-price mapping across the project.

> **2026-09 update:** authorization moved to a key-based model — roles carry
> editable permission bundles (`users_role_permissions`), resolved by
> `core.permissions.effective_keys` and managed on the `/Role_Permissions`
> admin page. Role/permission behaviour described below still holds, but read
> [`PERMISSIONS.md`](PERMISSIONS.md) first for the current authority model,
> the API, and the migration/cleanup contract.

## 1) Why this module is important

`orders` (workflow engine) depends on this module for:

- Authentication (`/api/auth/login/`)
- `User` role/filtering (`admin`, `manager`, `approver`, `billing`, `auditor`, `staff`)
- User ↔ party mapping (`user_party_assignments`) for visibility scopes in order listing
- Party ↔ product pricing (`party_product_assignments`) used during scheme/rate logic

If you understand `users`, you understand who can see/update what in `orders`.

## 2) Files in this module

- [users/models.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\users\models.py)
- [users/serializers.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\users\serializers.py)
- [users/views.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\users\views.py)
- [users/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\users\urls.py)
- [users/migrations/*.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\users\migrations)

Mounted by:
- [OMS-Backend/OMS/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\OMS\urls.py) as `/api/auth/`

## 3) Data models (DB tables and roles)

Primary tables (`db_table` values):

- `users_user` (custom user)
- `users_role` (Role master)
- `companies`
- `main_groups`
- `states`
- `orders`/`orders` module uses `orders.categories` (FK from `users.User.category`)
- `user_party_assignments`
- `party_product_assignments`
- `users_user_states` (legacy join view/table)
- `scheme_product`

### User model (`users.User`)

Core fields:
- `id`
- `name`, `username` (unique), `email`, `phone`
- `role` -> FK to `users_role`
- `company`, `main_group`, `state`, `category`, `is_active`
- `main_groups` (M2M to `MainGroup`)  
- `states` (M2M to `State`)
- audit fields: `created_by`, `updated_by`, `created_at`, `updated_at`
- password hash stored in `AbstractUser.password`

Important note: migration history shows role and mapping style evolved over time, but current model is FK-based role + relation fields above.

### Party/User assignment tables

- `user_party_assignments`
  - Columns: `user_id`, `card_code`, `category`, `is_active`, `assigned_by`, timestamps
  - Constraint: unique per `(user, card_code, category)`

- `party_product_assignments`
  - Columns: `card_code`, `item_code`, `category`, `basic_rate`, `is_active`, `scheme`, `is_scheme`, `assigned_by`
  - Constraint: unique per `(card_code, item_code, category)`

- `scheme_product`
  - Schema stores discount/promotional schemes and optional mapping to product code/state code.

## 4) Express-style mapping

Think like:

- `views.py` classes = controller functions
  - `LoginView` → `authController.login`
  - `CreateUserView` → `userController.create`
  - `UserDetailView` → `userController.getById` + `userController.update`
- `serializers.py` = DTO/validation layer (similar to `zod`/`Joi`)
- M2M helpers = relation management in service layer

Routes are like Express router at:
- `app.use('/api/auth', usersRouter)`
- plus per-endpoint handlers under this router.

## 5) JWT behavior in this module

Login endpoint issues JWT tokens:

- File: [users/views.py#LoginView](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\users\views.py)
- Uses:
  - [users/serializers.py#LoginSerializer](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\users\serializers.py)
  - Django `authenticate` (username/password check)
  - `RefreshToken.for_user(user)` from `rest_framework_simplejwt`
- Global auth config is in:
  - [OMS-Backend/OMS/settings.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\OMS\settings.py) (`JWTAuthentication`)

Returned token shape:

- `access`, `refresh`
- user object via `UserSerializer`

## 6) Endpoints (`/api/auth/...`) and what each reads/writes

Source routes:
[users/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\users\urls.py)

### 1. Authentication / profile

- `POST /api/auth/login/`
  - Handler: `LoginView.post`
  - Reads: `users_user` by username in `LoginSerializer.validate`
  - Writes: none (read-only)
  - Returns: JWT tokens + user data

- `GET /api/auth/profile/`
  - Handler: `ProfileView.get`
  - Permission: authenticated
  - Reads: current authenticated user row and related company/main group/state/M2M
  - Writes: none

### 2. Master data read APIs

- `GET /api/auth/states/`
  - Handler: `StateListView`
  - Reads: `states` where `is_active = true`

- `GET /api/auth/companies/`
  - Handler: `CompanyListView`
  - Reads: `companies` where `is_active = true`

- `GET /api/auth/mainGroup/`
  - Handler: `MainGroupListView`
  - Reads: `main_groups` where `is_active = true`

- `GET /api/auth/categories/`
  - Handler: `CategoryListView`
- `GET /api/auth/roles/`
  - Handler: `RoleListView`

### 3. User CRUD

- `POST /api/auth/users/create/`
  - Handler: `CreateUserView.post`
  - Writes:
    - INSERT into `users_user`
    - optional insert rows in M2M join tables:
      - `users_user_main_groups`
      - `users_user_states`
    - optional legacy/user-state rows in `users_user_states` using `UserState.objects.create`
  - Password set via `set_password` (hashed)

- `GET /api/auth/users/list/`
  - Handler: `UserListForAssignmentView.get`
  - Reads: list `users_user` filtered active + prefetch role/company/main_groups/state

- `GET /api/auth/users/<user_id>/`
  - Handler: `UserDetailView.get`
  - Reads: one user row

- `PUT /api/auth/users/<user_id>/`
  - Handler: `UserDetailView.put`
  - Writes:
    - UPDATE `users_user` fields (`name`, `username`, `email`, etc.)
    - updates M2M sets (`main_groups`, `states`) if passed
    - updates `UserState` rows for explicit `states` list
    - updates password when provided

- `POST /api/auth/users/<user_id>/delete/`
  - Handler: `DeleteUserView.post`
  - Writes: soft delete (`is_active = false`) on `users_user`

### 4. User ↔ Party assignment

- `GET /api/auth/users/<int:user_id>/parties/`
  - Handler: `UserPartiesView.get`
  - Reads:
    - `user_party_assignments` where user is active
    - resolve `sap_sync.Party` for metadata

- `GET /api/auth/parties/<card_code>/users/`
  - Handler: `PartyUsersView.get`
  - Reads:
    - `sap_sync.Party` for `card_code`
    - `user_party_assignments` filtered active + optional category

- `POST /api/auth/assign-parties/`
  - Handler: `AssignPartiesView.post`
  - Writes:
    - UPSERT-like `UserPartyAssignment` (`update_or_create`) for requested party selections
    - soft-deactivate removed assignments (`is_active = false`)

- `POST /api/auth/remove-party/`
  - Handler: `RemovePartyAssignmentView.post`
  - Writes:
    - set `is_active = false` for one `user_party_assignments` row

### 5. Party ↔ Product assignment

- `GET /api/auth/parties/<card_code>/products/`
  - Handler: `PartyProductsView.get`
  - Reads:
    - `party_product_assignments` where active + optional category
    - resolves `sap_sync.Product` for metadata

- `POST /api/auth/party-product/add/`
  - Handler: `AssignProductToPartyView.post`
  - Writes:
    - `PartyProductAssignment.update_or_create(...)`
    - sets `basic_rate`, `is_active = true`, `assigned_by`

- `POST /api/auth/party-product/bulk-add/`
  - Handler: `BulkAssignProductsToPartyView.post`
  - Writes many `update_or_create` rows for one party + many products

- `POST /api/auth/bulk-party/assign-products/`
  - Handler: `BulkAssignPartyToProductView.post`
  - Writes many `update_or_create` rows across many parties/products

- `POST /api/auth/party-product/update-rate/`
  - Handler: `UpdateProductRateView.post`
  - Writes:
    - UPDATE `party_product_assignments.basic_rate` on one active assignment

- `POST /api/auth/party-product/remove/`
  - Handler: `RemoveProductFromPartyView.post`
  - Writes:
    - soft-deactivate assignment (`is_active = false`)

## 7) Request/response examples (high-level behavior)

### Login

`POST /api/auth/login/`

```json
{
  "username": "admin1",
  "password": "secret"
}
```

Response includes:
- `tokens.access`
- `tokens.refresh`
- `data.user`

### Assign many products to one party

`POST /api/auth/party-product/bulk-add/`

```json
{
  "card_code": "C001",
  "products": [
    {"item_code": "FG001", "category": "OIL", "basic_rate": 140},
    {"item_code": "FG010", "category": "MART", "basic_rate": 90}
  ]
}
```

### Assign many parties/products

`POST /api/auth/bulk-party/assign-products/`

```json
{
  "party_selections": [{"card_code":"C001"},{"card_code":"C002","category":"OIL"}],
  "products": [{"item_code":"FG001","category":"OIL","basic_rate":120}]
}
```

## 8) ORM → SQL mental model for onboarding

### Login check

```sql
SELECT * FROM users_user
WHERE username = :username
AND is_active = TRUE;
```

### Create user with relations

```sql
INSERT INTO users_user (...) VALUES (...);
INSERT INTO users_user_main_groups (user_id, maingroup_id) VALUES (...);
INSERT INTO users_user_states (user_id, state_id) VALUES (...);
INSERT INTO users_user_states (user_id, state_id, maybe) VALUES (...); -- explicit helper table in this code
```

### Assign party to user

```sql
SELECT * FROM user_party_assignments
WHERE user_id = :user_id
  AND card_code = :card_code
  AND category = :category
  AND is_active = true;

INSERT INTO user_party_assignments (...) 
  ON CONFLICT (...) DO UPDATE SET is_active = true;
```

### Party-product pricing

```sql
INSERT INTO party_product_assignments (card_code, item_code, category, basic_rate, is_active, assigned_by_id)
VALUES (...)
ON CONFLICT (card_code, item_code, category)
DO UPDATE SET basic_rate = EXCLUDED.basic_rate, is_active = true;
```

## 9) Data access and role scope impact on `orders`

`orders` module relies on these keys:

- `UserPartyAssignment` to check which parties a user can work on
- `User.category` + `User.state(s)` filters when applying assignment logic
- `PartyProductAssignment.basic_rate` for pricing suggestions/validation
- `UserRole.name` for permissions in order screens

Important paths in `orders`:
- [orders/views.py#_get_base_orders](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\views.py)
- [orders/views.py#_get_rate_approval_reason](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\views.py)
- [orders/scheme_rules.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\scheme_rules.py) queries party/product assignment helpers from `users`.

## 10) Security and consistency notes for a new developer

Current notable behavior:
- Several endpoints are `AllowAny` even for write operations (e.g., create user, assign parties).  
  In a production hardening pass, these should likely become JWT-protected endpoints.
- Duplicate `UserPartiesView` class appears once in file (same behavior, different line region) — keep only one to avoid future maintenance confusion.
- `users_user_states` is configured as `managed = False` in model, but serializer writes manually maintain consistency.

## 11) Migration landmarks to understand table evolution

- `0001_initial.py` — initial custom user, company/main_group/state tables
- `0002_alter_state_code_remove_user_company_and_more.py` — moved some user relations to M2M (later changed again)
- `0003_remove_user_role.py` / `0004_userrole_user_role.py` — role moved from plain field to FK table
- `0005_partyproductassignment_userpartyassignment.py` — introduced party assignment tables
- `0009_userstate_alter_schemeproduct_item_code.py` — introduces `users_user_states` helper table model
- `0010_remove_schemeproduct_state_schemeproduct_state_code_and_more.py` — `scheme_product` moved to string `state_code`
- `0011_userpartyassignment_category.py` — adds category to user-party assignment
- `0013_user_category.py` — adds category to user
- `0014_alter_company_id...` and `0015...` — PK type normalization

## 12) Where to continue next

After this doc, the next useful module to understand is `sap_sync`:
- master data comes from `sap_sync.models.Party`, `Product`, `PartyAddress`
- pushes logs and sync scheduling connect directly to backend runtime behavior

That should be read right after users because this module is the data backbone for permissions and assignments.
