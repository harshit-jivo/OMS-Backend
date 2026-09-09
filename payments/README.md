# Payments module — deployment notes

Receive Payment + Bank Deposit, with a generic multi-level approval engine.

Apps added: `core`, `approvals`, `attachments`, `payments`.

---

## 1. Environment variables

Add to `.env`:

```ini
# --- Attachment storage -----------------------------------------------------
# Same shared-folder strategy as EINV_QR_SAVE_DIR. Files are written FLAT into
# these directories under a generated UUID name — no year/month/company
# sub-folders, no Django MEDIA_ROOT.
PAYMENTS_IMAGES=\\JIVO-APP\Payments\Receive_Payments
DEPOSIT_PAYMENTS_IMAGES=\\JIVO-APP\Payments\Deposit_Payments

# Optional. If omitted these fall back to EINV_QR_SMB_USERNAME / _PASSWORD,
# so if the QR share already works no extra config is needed.
# PAYMENTS_SMB_USERNAME=DOMAIN\svc_oms
# PAYMENTS_SMB_PASSWORD=...
```

Both directories must exist on the share (or be creatable by the service
account) before the first upload.

> ### ⚠️ Pre-existing issue, unrelated to this module
> `OMS/settings.py:152` reads `config('HANA_DB_NAME')` but `.env` only defines
> `HANA_DB_OIL_NAME`. Django will not start without it. Either add
> `HANA_DB_NAME=<oil db>` to `.env`, or change the setting to read the key that
> already exists. Every command below was verified by passing it explicitly.

---

## 2. Migrate

```bash
python manage.py migrate core
python manage.py migrate approvals
python manage.py migrate attachments
python manage.py migrate payments
```

---

## 3. Seed the required master data

Nothing works until these three are populated (Django admin is fine).

**a) SAP company mapping** — Admin → Payments → *SAP company mappings*.
One row per company. This replaces `resolve_company_db_for_order`, which only
handles BEVERAGES and silently routes MART to the OIL database.

| company | company_db | hana_schema |
|---|---|---|
| OIL | JIVO_OIL_HANADB | JIVO_OIL_HANADB |
| BEVERAGES | JIVO_BEVERAGES_HANADB | JIVO_BEVERAGES_HANADB |
| MART | *(the real MART db)* | *(same)* |

`HANA_COMPANY_DB_MART` is referenced at `hana/services/connection.py:74,90`
but has never been defined in settings, so MART data is invisible today. This
table is where that gets fixed.

**b) Bank accounts** — Admin → Payments → *Bank accounts*. Needs the real SAP
GL codes (`_SYS…`); the payload builder maps cash → `CashAccount`,
UPI → `TransferAccount`, cheque → `CheckAccount`.

**c) Approval workflow** — Admin → Approvals → *Approval workflows*.
One active workflow per (document type, company), then its levels in order:

```
ApprovalWorkflow(code='PAYMENT_OIL_V1', document_type='PAYMENT', company='OIL')
  ApprovalLevel(sequence=1, name='Accountant Review', role=<Accountant>)
  ApprovalLevel(sequence=2, name='Manager Approval',  role=<Manager>)
```

Use `GET /api/approvals/workflows/<id>/preview/?company=OIL` to see the exact
ladder and the resolved approver names before going live — it flags any level
with nobody able to approve it.

---

## 4. Run the SAP outbox worker

Approval does **not** post to SAP inline. It writes a `SapOutbox` row in the
same transaction, and this drains it:

```bash
python manage.py drain_sap_outbox
```

Schedule every 15–30s (APScheduler, Task Scheduler or cron). Safe to run
concurrently — claiming uses `select_for_update(skip_locked=True)` and every
post is guarded by an idempotency probe.

> ### ⚠️ Two SAP prerequisites before enabling posting
> 1. **Create the UDFs** `U_OMS_IDEM` (alphanumeric 36) and `U_OMS_REF`
>    (alphanumeric 50) on **ORCT and ODPS in all three company DBs**.
>    Without a queryable idempotency field an ambiguous timeout cannot be
>    resolved, and the choice becomes duplicate payment or lost payment.
> 2. **Configure `CACHES` (Redis).** With no `CACHES` block Django uses
>    `LocMemCache`, so each Gunicorn worker holds its own SAP session and
>    cache invalidation reaches only one worker.

---

## 5. API surface

| Endpoint | Purpose |
|---|---|
| `GET /api/payments/companies/` | Step 1 of the cascade |
| `GET /api/payments/parties/?company=OIL&search=` | Step 2 — scoped, paginated |
| `GET /api/payments/open-invoices/?company=OIL&card_code=X` | Step 3 — **live** SAP |
| `GET /api/payments/collection-persons/?company=OIL` | "Received From" people |
| `GET /api/payments/bank-accounts/?company=OIL` | Deposit targets |
| `POST /api/payments/receipts/` | Create (nested methods + denominations + allocations) |
| `POST /api/payments/receipts/<id>/submit/` | Enter approval |
| `GET /api/payments/receipts/<id>/history/` | Lifecycle log |
| `POST /api/payments/receipts/<id>/attachments/` | Upload (multipart `file`) |
| `GET /api/payments/depositable-receipts/?company=OIL` | Posted, not yet banked |
| `POST /api/payments/deposits/` | Create from receipt ids |
| `POST /api/payments/deposits/<id>/submit/` | Enter approval |
| `GET /api/payments/attachments/<id>/download/` | Permission-checked stream |
| `GET /api/approvals/inbox/` | This user's pending queue |
| `POST /api/approvals/requests/<id>/act/` | `{"decision":"APPROVE\|REJECT\|CANCEL","remarks":""}` |
| `GET /api/approvals/workflows/<id>/preview/` | Ladder + resolved approvers |

All responses use `{success, message, data}`.

---

## 6. Design notes

**"Company" is the SAP category.** `OIL` / `BEVERAGES` / `MART` map 1:1 to
company DBs, and the *same* `card_code` is a different business partner in
each — `CUSTA000878` is PURAN STORE under OIL and A ONE BEVERAGES under
BEVERAGES (`orders/views.py:2732-2733`). Every query and payload therefore
carries `(card_code, company)` as a pair, and the party endpoint **requires**
a company.

**Attachments** are written flat to the configured share via `smbclient`,
mirroring `einvoice/services.py:386-455` (temp-write then rename). The database
stores metadata only: `stored_name`, `original_name`, `attachment_type`,
uploader and timestamps. Validation: `jpg/jpeg/png/pdf`, ≤ 5 MB, with a
magic-byte check so a `.jpg` that is really HTML is rejected. Files are never
served from a guessable URL — the download view checks permission, and the
network path is never exposed to the client.

**Idempotency.** Each document gets a UUID at creation, before any network
call, sent to SAP as `U_OMS_IDEM`. Before every retry the worker queries SAP
for that key; if the document is already there it adopts the DocEntry instead
of posting again. If the *verification query itself* fails the row goes to
`NEEDS_REVIEW` rather than guessing — the same three-state discipline as
`invoice/views.py:84-102`.

**Money invariants are DB constraints**, not just serializer rules — verified
to reject negative totals, over-allocation, over-deposit and a short deposit
with no reason. The project previously had zero `CheckConstraint`s, so any
write path that bypassed the serializer bypassed validation entirely.
