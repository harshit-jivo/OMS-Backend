# OMS-Backend `serviceLayer` Module - Beginner Friendly Deep Notes (Express + DB lens)

This module is the SAP Service Layer gateway for OMS.

Think of it as:
- `serviceLayer/service.py` = SAP session/auth manager
- `serviceLayer/views.py` = route handlers (`invoice`, `draft`, `draft-action`)
- `serviceLayer/urls.py` = URL binding
- no local persistent tables are written by this app

Mounted path:

- [OMS-Backend/OMS/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\OMS\urls.py)
  - `path('api/service-layer/', include('serviceLayer.urls'))`

## 1) Files in this module

- [serviceLayer/service.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\serviceLayer\service.py)
- [serviceLayer/views.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\serviceLayer\views.py)
- [serviceLayer\urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\serviceLayer\urls.py)
- [serviceLayer\models.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\serviceLayer\models.py) (placeholder, no used models)
- [serviceLayer\apps.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\serviceLayer\apps.py)

## 2) Why this module exists

`serviceLayer` gives OMS direct pass-through access to SAP Business One Service Layer for:

- creating invoice documents
- creating draft documents
- fetching draft by id
- approving/rejecting/acting on approval request for a draft

It is a thin integration layer; most business orchestration and logging still happen in other modules (`orders`, `invoice`).

## 3) Routes (`/api/service-layer/`)

From [serviceLayer/urls.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\serviceLayer\urls.py):

- `POST /api/service-layer/invoice/` -> [SAPInvoiceCreateView](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\serviceLayer\views.py)
- `POST /api/service-layer/draft/` -> [DraftView](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\serviceLayer\views.py)
- `GET /api/service-layer/draft/?draft_id=<id>` -> [DraftView](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\serviceLayer\views.py)
- `POST /api/service-layer/draft-action/?draft_id=<id>&status=<action>` -> [DraftActionView](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\serviceLayer\views.py)

## 4) SAPServiceLayerManager behavior (session layer)

Implemented in [serviceLayer/service.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\serviceLayer\service.py):

- `get_session()`:
  - Reads/writes shared session cookies from Django cache:
    - `b1_session` (SAP session token)
    - `route_id` (ROUTEID)
  - If cached values exist, returns a `requests.Session` with those cookies.
  - If not, performs login request:
    - `POST {HANA_SERVICE_LAYER_URL}/Login`
    - payload: `CompanyDB`, `UserName = HANA_USERNAME`, `Password = HANA_PASSWORD`
  - On success, saves cache with timeout `SessionTimeout - 60`.
  - On login failure, raises exception.
- `get_session_for(username, password)`:
  - Same login flow but for explicit user credentials.
  - Returns a fresh session with `B1SESSION` and `ROUTEID`.
  - Used where SAP action must run under approver identity.
- `clear_session()`:
  - drops `b1_session` and `route_id` from cache.

TLS policy:
- `session.verify = False` everywhere in this module.
- SSL warning suppression is global in this file.
- In `settings.py`, [HANA_SSL_VERIFY](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\OMS\settings.py) is also present and used in other SAP calls.

## 5) Endpoint logic in detail

### 5.1 `POST /invoice/?type=...`

Handler class: [SAPInvoiceCreateView](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\serviceLayer\views.py)

- Expects request query param:
  - `type` required (`DRAFT` or `INVOICE`)
- if `type == DRAFT`:
  - logs in with `HANA_USERNAME/HANA_PASSWORD`
- else:
  - logs in with `SAP_APPROVER_USER/SAP_APPROVER_PASSWORD`
- Calls `session.post("{HANA_SERVICE_LAYER_URL}/Invoices", json=invoice_payload)`
- Timeout: `20s`
- On `401`:
  - clears cached session and retries login call
- On `200` or `201`:
  - returns SAP response JSON, `201 Created`
- On other status:
  - returns `{error: "SAP Error", details: sap_response.json()}`

### 5.2 `POST /draft/`

Handler class: [DraftView.post](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\serviceLayer\views.py)

- Always uses drafter credentials (`HANA_USERNAME/HANA_PASSWORD`)
- Calls `POST {HANA_SERVICE_LAYER_URL}/Drafts`
- Same 20s timeout, 401 retry pattern
- Returns SAP JSON with 201 on success (200/201)

### 5.3 `GET /draft/?draft_id=...`

Handler class: [DraftView.get](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\serviceLayer\views.py)

- Requires `draft_id`
- Calls `GET {HANA_SERVICE_LAYER_URL}/Drafts({draft_id})`
- Uses shared session from cache (`SAPServiceLayerManager.get_session()`)
- 401 retry logic same as above

### 5.4 `POST /draft-action/?draft_id=...&status=...`

Handler class: [DraftActionView](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\serviceLayer\views.py)

Flow:

1. Requires `draft_id`, `status` (status gets interpolated as `ard{status}` into payload)
2. Login as approver account:
   - `SAP_APPROVER_USER`, `SAP_APPROVER_PASSWORD`
3. Lookup approval request:
   - `GET /ApprovalRequests?$filter=DraftEntry eq {draft_id}&$select=Code`
4. If no approval entry found → `404`
5. Patch approval decision:
   - `PATCH /ApprovalRequests({approval_code})`
   - body: `{"ApprovalRequestDecisions":[{"Status":"ard<status>","Remarks":"Approved via OMS Portal"}]}`
6. Success on status `200|201|204` -> returns `{status: action_status.lower(), draft_id: draft_id}`

Potentially important:
- Current string logic implies expected statuses like:
  - `status=accept` => `ardaccept`
  - `status=reject` => `ardreject`
- This depends on SAP approval status codes used by environment.

## 6) Data flow and external interactions

`serviceLayer` has no Django models in this app (except placeholder), so writes are external to SAP only.

Key external dependencies:
- SAP Service Layer URL: [HANA_SERVICE_LAYER_URL](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\OMS\settings.py)
- Company DB for session login: [HANA_COMPANY_DB](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\OMS\settings.py)
- Approver credentials:
  - [SAP_APPROVER_USER](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\OMS\settings.py)
  - [SAP_APPROVER_PASSWORD](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\OMS\settings.py)

### Typical integration path in app

- `orders` can use this session manager for:
  - quotation cancellation and status checks (`SAPServiceLayerManager.get_session`)
  - cancel endpoint is in [orders/views.py](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\orders\views.py)
- `invoice` module stores internal invoice records and references (DB side), while Service Layer can be used for document creation in SAP.

## 7) Permission notes

`serviceLayer/views.py` does not set `permission_classes`, so behavior depends on global DRF defaults:
- if no default authentication/permission is forced, routes can become publicly callable.
- In current settings, JWT authentication is configured globally, but no default permission class is explicitly enforced.

Also:
- `SAPInvoiceCreateView` has user role/claims checks only through request context not implemented in this file.
- `SAP` credentials are read from env and not validated in the endpoint before outbound call.

## 8) Security/operational notes for onboarding

1. **Session caching is global, not user-scoped** for `get_session()`:
   - shared SAP session can be reused across users.
2. **No SSL verification**: session cookies and HTTP requests set `verify=False`.
3. **Error handling is coarse-grained**:
   - broad `except Exception` with 500 response.
4. **Bug-prone pattern in draft create retry path**:
   - fallback path calls `SAPServiceLayerManager.get_session(...)` but only `get_session` is defined.
   - this branch can throw a runtime attribute error if token expired with 401 and no retry cache.
5. **Retry logic** exists for 401 only; other SAP errors pass through directly.

## 9) SQL/ORM equivalents (for orientation)

No local SQL write/read for this app’s core actions.

Conceptually, this is direct API write-through:

```text
HTTP POST /Invoices(Draft/Invoice Payload)  -> SAP Service Layer create
HTTP POST /Drafts(DraftPayload)           -> SAP Service Layer create
HTTP GET  /Drafts(<id>)                   -> SAP Service Layer read
HTTP GET  /ApprovalRequests(...DraftEntry) -> SAP Service Layer read
HTTP PATCH /ApprovalRequests(<code>)       -> SAP Service Layer update/action
```

## 10) Next: recommended next module to study

Next documentation would logically be `invoice`, because it is the internal audit trail side of SAP document handoff:
- [invoice-module-workflow.md](C:\Users\Rohit Rathod\Desktop\OMS\OMS-Backend\docs\invoice-module-workflow.md)

If you want, I’ll create that next in the same structure now. 
