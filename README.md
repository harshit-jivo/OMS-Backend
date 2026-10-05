# OMS Backend

Django REST backend for the Order Management System. The project handles user authentication, order creation and approval workflows, SAP master-data sync, sales quotation pushes, dashboards, notifications, and party/product assignment management.

## Tech Stack
- Python
- Django
- Django REST Framework
- Jivo Auth (auth.jivo.in) sign-in, via `jivo-auth-client`
- PostgreSQL
- SAP/HANA integration through SQL Server and SAP Service Layer
- APScheduler for scheduled sync jobs
- Optional local AI order summary support

## Project Structure

```text
OMS-Backend/
|-- manage.py
|-- OMS/                 # Django project settings, URLs, ASGI/WSGI
|-- users/               # Auth, users, roles, states, assignments
|-- orders/              # Orders, approvals, schemes, dashboards, notifications
|-- sap_sync/            # SAP sync, SAP models, service-layer integration
|-- login/               # Legacy/unused login app folder
`-- README.md
```

## Setup

Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
pip install -r requirements.txt
```

Create a `.env` file in the project root:

```env
DEBUG=true

DB_NAME=your_database_name
DB_USER=your_database_user
DB_PASSWORD=your_database_password
DB_HOST=localhost
DB_PORT=5432

HANA_SERVICE_LAYER_URL=https://your-sap-service-layer-url
HANA_USERNAME=your_hana_username
HANA_PASSWORD=your_hana_password
HANA_COMPANY_DB=your_company_db
HANA_COMPANY_DB_BEVERAGES=
HANA_WAREHOUSE_CODE=GP-FG
HANA_WAREHOUSE_CODE_BEVERAGES=
HANA_SSL_VERIFY=false
HANA_SSL_CA_BUNDLE=
HANA_CONNECT_TIMEOUT=15
HANA_READ_TIMEOUT=120

SAP_DB_HOST=your_sql_server_host
SAP_DB_PORT=1433
SAP_DB_NAME=your_sap_source_database
SAP_DB_USER=your_sap_db_user
SAP_DB_PASSWORD=your_sap_db_password
```

Run migrations:

```powershell
python manage.py migrate
```

Create an admin user if needed:

```powershell
python manage.py createsuperuser
```

Start the development server:

```powershell
python manage.py runserver
```

The API will be available at:

```text
http://127.0.0.1:8000/
```

## Main API Groups

### Auth and User Management

Base path: `/api/auth/`

- `GET /api/auth/profile/` (the signed-in OMS user: id, roles, pages)
- `GET /api/auth/jivo-users/` (admin: Jivo users with OMS access)
- `POST /api/auth/users/create/` (admin: give a Jivo user an OMS account)
- `GET /api/auth/states/`
- `GET /api/auth/companies/`
- `GET /api/auth/mainGroup/`
- `GET /api/auth/categories/`
- `POST /api/auth/users/create/`
- `GET /api/auth/users/list/`
- `GET /api/auth/roles/`
- `GET /api/auth/users/<user_id>/`
- `DELETE /api/auth/users/<user_id>/delete/`

### Orders

Base path: `/api/orders/`

- `GET /api/orders/parties/`
- `GET /api/orders/products/`
- `POST /api/orders/create/`
- `PUT/PATCH /api/orders/<order_id>/update/`
- `GET /api/orders/list/`
- `POST /api/orders/<order_id>/approve/`
- `POST /api/orders/<order_id>/reject/`
- `POST /api/orders/<order_id>/update-status/`
- `GET /api/orders/dashboard/`
- `GET /api/orders/dashboard/charts/`
- `GET /api/orders/notifications/`
- `GET/PATCH /api/orders/notifications/<pk>/`

### SAP Sync

Base path: `/api/sap/`

- `POST /api/sap/sync/all/`
- `POST /api/sap/sync/products/`
- `POST /api/sap/sync/parties/`
- `POST /api/sap/sync/addresses/`
- `POST /api/sap/sync/branches/`
- `GET /api/sap/products/`
- `GET /api/sap/parties/`
- `GET /api/sap/branches/`
- `GET /api/sap/logs/`
- `GET /api/sap/status/`
- `POST /api/sap/push-quotation/`
- `POST /api/sap/approve-order/`

## Authentication

Users sign in at Jivo Auth, the central sign-in service, not at OMS:
`POST https://auth.jivo.in/api/v1/auth/login/` with `{email, password, device_name}`
returns `{access, refresh}`, and `POST https://auth.jivo.in/api/v1/auth/refresh/`
renews them. Send the access token on every OMS request:

```http
Authorization: Bearer <Jivo access token>
```

OMS verifies it locally and resolves the OMS user whose `auth_id` is the
token's subject; then call `GET /api/auth/profile/` for the OMS user. Accounts
and passwords are managed in Jivo Auth; OMS keeps roles, scope and every
business record. The old `POST /api/auth/login/` answers `410 Gone`. Settings:
`JIVO_AUTH_*` in `.env.example`. Full details: `docs/jivo-auth-integration.md`.

## Development Commands

```powershell
python manage.py makemigrations
python manage.py migrate
python manage.py test
python manage.py runserver
```

## Notes

- `.env`, `.venv`, Python cache files, SQLite databases, static build output, and media files are ignored by `.gitignore`.
- `DEBUG` is currently forced to `True` in `OMS/settings.py`; update this before production deployment.
- `SECRET_KEY` is currently hardcoded in `OMS/settings.py`; move it to `.env` before production deployment.
