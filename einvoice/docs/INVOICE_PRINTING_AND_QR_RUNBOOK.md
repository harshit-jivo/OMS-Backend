# OMS Invoice Printing, Crystal Report & QR — Operations Runbook

Practical, end-to-end reference for **how a GST tax invoice gets printed from OMS
with its IRN + signed QR**, where every piece lives, and how to fix it when the
QR (or the whole PDF) fails. Complements the API-level
[NIC e-Invoice + e-Way Bill reference](./NIC_EINVOICE_EWAYBILL_REFERENCE.md)
(IRN generation, schema, error codes, crypto).

> Last verified: 2026-07-29. Server/paths/credentials can drift — verify before acting.
>
> **2026-07-29 changes:** the flow is now **multi-company** (OIL / BEVERAGE / MART) —
> see [§4](#4-multi-company-routing-oil--beverage--mart). The SP no longer requires a
> QR to return the IRN/Ack fields ([§3](#3-the-stored-procedure-oms_sp_gst_invoice)),
> and OMS writes QR PNGs into **per-company folders** ([§2](#2-irn--qr--data-sources)).

---

## 0. TL;DR — the one-screen mental model

```
Browser (OMS UI)
   │  GET http://103.89.45.75:8008/api/billprint/{DocEntry}
   ▼
IIS site "CrystalReportService"  (server .75, C:\inetpub\CrystalReportService)
   │  renders Reports\BillPrint.rpt  → PDF
   │  DB via ODBC DSN "HANA_LIVE_OIL" → JIVO_OIL_HANADB, user DSR
   ▼
Stored proc  OMS_SP_GST_INVOICE(DocKey)   (aliased UNE_SP_GST_INVOICE in the .rpt)
   │  returns all invoice fields + "UNE QR Code" = a UNC path to the QR PNG
   ▼
Crystal picture object (Graphic Location formula = {…UNE QR Code})
   │  opens the UNC at render time AS THE APP-POOL WINDOWS IDENTITY
   ▼
QR PNG on  \\JIVO-APP (=20.20.45.25)  →  embedded in the PDF
```

**The #1 gotcha:** Crystal loads the QR image using the *IIS worker's Windows
identity*, not the DB user. If that identity can't read the `\\JIVO-APP` share,
the QR is blank (or the render 500s) — even though every other field is correct.

---

## 1. Servers & components

| Thing | Where | Notes |
|---|---|---|
| SAP B1 server / app host | **103.89.45.75** (internal `20.20.45.75`, host `Jivo`) | SSH `Admin` / `English@jivo`. Runs SAP B1 + IIS + many apps under `C:\LiveProjects`. |
| File server (QR bitmaps) | **JIVO-APP = 20.20.45.25** | Only reachable from inside (from .75). Shares need login `OMS` / `Jivo@2026`. |
| HANA DB | **103.89.45.192:30015** | Schemas incl. `JIVO_OIL_HANADB` (= "hana_live_oil"). User `DSR` / `Jivo@2025`. |
| Crystal render service | IIS site **CrystalReportService**, `C:\inetpub\CrystalReportService`, port **8008** | .NET Framework 4.8 Web API 2 + Crystal runtime 13.0.x. Source: `C:\LiveProjects\Crystal Report Utility`. |
| OMS backend (Django) | `C:\LiveProjects\OMS\Backend` (dev) / deployed sites on .75 | Generates IRNs, writes QR PNGs to the share. |
| Frontend print call | `VITE_BILLPRINT_API_URL = http://103.89.45.75:8008` | `/api/billprint/{docEntry}`. |

The report `.rpt` is edited in **SAP Crystal Reports Designer**; the deployed copy
that IIS actually serves is `C:\inetpub\CrystalReportService\Reports\BillPrint.rpt`
(keep it in sync with the source copy after editing).

---

## 2. IRN & QR — data sources

Two tables can hold an invoice's IRN + QR, **per HANA company schema**. Each company
keeps its bitmaps in its own folder — nothing is shared between companies:

| Table | Populated by | QR path it stores |
|---|---|---|
| `@UTL_MDEXTH` | SAP e-invoice **add-on** (invoices done in SAP) | `C:\SAP Attachments\Jivo {Oil\|Beverages\|Mart}\Bitmaps\<hash>.png` → rewritten by the SP to `\\JIVO-APP\Jivo {Oil\|Beverages\|Mart}\Bitmaps\<hash>.png` |
| `OMS_IRN_LOG` | **OMS** e-invoice flow (`einvoice` app) | `\\JIVO-APP\OMS_Attachments\{OIL\|BEVERAGE\|MART}_ATTACHMENTS\Bitmap\<irn>.png` (stored as a full UNC — the SP passes it through **unchanged**) |

Per-company destinations (verified reachable + writable from .75 as user `OMS`):

| Company DB | SAP add-on QR (read) | OMS QR (write) |
|---|---|---|
| `JIVO_OIL_HANADB` | `\\JIVO-APP\Jivo Oil\Bitmaps` | `\\JIVO-APP\OMS_Attachments\OIL_ATTACHMENTS\Bitmap` |
| `JIVO_BEVERAGES_HANADB` | `\\JIVO-APP\Jivo Beverages\Bitmaps` | `\\JIVO-APP\OMS_Attachments\BEVERAGE_ATTACHMENTS\Bitmap` |
| `JIVO_MART_HANADB` | `\\JIVO-APP\Jivo Mart\Bitmaps` | `\\JIVO-APP\OMS_Attachments\MART_ATTACHMENTS\Bitmap` |

> ⚠️ The folder is `MART_**ATTACHMENTS**` (two T's). A single-T spelling silently
> breaks Mart QR saves — the hook is best-effort and only logs.

- Both tables store the **same** signed-QR image content per invoice, just under
  different shares. OMS names its file `{irn}.png` (`EINV_QR_FILENAME`).
- `OMS_IRN_LOG` exists in `JIVO_OIL_HANADB`, `TEST_OIL_*`, `JIVO_BEVERAGES_HANADB`,
  `JIVO_MART_HANADB` (37 cols + IDENTITY PK). Key columns:
  `U_UTL_BaseEntry` (= invoice `OINV.DocEntry`), `U_UTL_DocType` (13 = A/R invoice),
  `U_UTL_IST` ('S' = success), `U_UTL_QRPT` (QR path), `U_UTL_IRN`, `U_UTL_AckNo`,
  `U_UTL_IRNGENDT`, `Canceled`.
- IRN itself = `SHA-256(SupplierGSTIN + FY + DocType + DocNo)` — see the
  [NIC reference](./NIC_EINVOICE_EWAYBILL_REFERENCE.md) §2 for generation, crypto,
  and how OMS stores the NIC response into `OMS_IRN_LOG`.
- OMS writes its QR PNG to the share with **explicit SMB creds**
  (`EINV_QR_SMB_USERNAME` / `EINV_QR_SMB_PASSWORD` in `.env`) via `smbclient`
  (`einvoice/services.py::_save_png_smb`). **These creds are WRITE-only for QR
  generation — they are NOT what the printing/Crystal path uses.**
- The folder is chosen by company at write time:
  `einvoice/services.py::qr_dir_for_company(company_db)` → `settings.EINV_QR_SAVE_DIRS`
  (override per company with `EINV_QR_SAVE_DIR_OIL` / `_BEVERAGE` / `_MART`, or move
  the base with `EINV_QR_ROOT`). An unknown company falls back to the legacy single
  `EINV_QR_SAVE_DIR`. **The path actually written is what gets stored in
  `U_UTL_QRPT`**, so the report always reads back the exact file that was written.
- Verify all three folders end-to-end (run **on .75**):
  `python manage.py test_qr_share` — probes every configured company folder
  (write → read back → delete).

---

## 3. The stored procedure `OMS_SP_GST_INVOICE`

File: [`einvoice/sql/oms_sp_gst_invoice.sql`](../sql/oms_sp_gst_invoice.sql).
Derived from `CRYSTAL_AR_INVOICE_ITEMS`; signature `(IN DOCKEY INT)`. The `.rpt`
references it under the alias **`UNE_SP_GST_INVOICE`**.

What it does differently from the base proc — it resolves IRN/QR from a **ranked
UNION** of the two sources (5 places in the proc):

- **Priority:** `@UTL_MDEXTH` first (`SRC_PRIO = 1`), `OMS_IRN_LOG` fallback
  (`SRC_PRIO = 2`). `ROW_NUMBER() OVER (PARTITION BY BaseEntry, DocType
  ORDER BY SRC_PRIO, IRNGENDT DESC)` → exactly one row per invoice.
- **Per-source QR path resolution** (inside each UNION branch, so the winning row
  already carries the right share):
  - `@UTL_MDEXTH`: `REPLACE("U_UTL_QRPT",'C:\SAP Attachments\','\\JIVO-APP\')`.
    Only the **root** is swapped — the company folder is preserved, so the same
    generic rewrite works for all three companies
    (`…\Jivo Oil\…`, `…\Jivo Beverages\…`, `…\Jivo Mart\…`) because the share names
    on JIVO-APP match SAP's folder names exactly.
  - `OMS_IRN_LOG`: left as-is (already a full per-company UNC).
- Output columns preserved incl. `"LICENSE FSSAI"`, `"Customer Fassai No"`,
  `"UNE QR Code"`, `"UNE IRN No"`, `"UNE Ack no"`, `"UNE Ack dt"`.

### 2026-07-29 — IRN/Ack no longer gated on the QR
Previously the `"UNE IRN No"` / `"UNE Ack no"` / `"UNE Ack dt"` subqueries each
carried `IFNULL("U_UTL_QRPT",'') <> ''` (in both the inner source filters **and** the
outer `WHERE`). An invoice with an IRN but **no QR yet** therefore printed with
blank IRN/Ack/AckDate. That condition was **removed from those three subqueries**.

The same QR condition was also removed from **`"Blank for Billing"`**, which drives
the report's *"e-invoice not generated"* label — it is now decided by the **IRN**,
not the QR. Effect: an invoice prints as a full tax invoice as soon as an IRN
exists, even before its QR is written.

> Still QR-gated (correctly): the `"UNE QR Code"` subquery itself — an image needs a path.

### Deploy / redeploy the SP
The proc must exist in **every** company schema that prints invoices — it is
deployed to `JIVO_OIL_HANADB`, `JIVO_BEVERAGES_HANADB` and `JIVO_MART_HANADB`
(identical source; it resolves tables against whatever schema it is created in).

⚠️ Two gotchas on this HANA server:
- **`DROP PROCEDURE … IF EXISTS` is not supported** (syntax error 257).
- The proc is owned by **`SYSTEM`**, so `DSR` gets *insufficient privilege* (258) on a
  bare `DROP`. **`CREATE OR REPLACE PROCEDURE` works** and is the way to redeploy.

```sql
SET SCHEMA "JIVO_OIL_HANADB";               -- proc uses unqualified table names
CREATE OR REPLACE PROCEDURE "OMS_SP_GST_INVOICE" (IN DOCKEY INT) ... END
```

Safe pattern used for deploys: first compile the new body under a **temp name** to
validate it, drop the temp, then `CREATE OR REPLACE` the real one — so a syntax
error can never leave production without a procedure.

Quick sanity check per company:
```sql
CALL "JIVO_OIL_HANADB"."OMS_SP_GST_INVOICE"(77202);
```
`"UNE IRN No"` / `"UNE Ack no"` / `"UNE Ack dt"` populate whenever an IRN exists;
`"UNE QR Code"` is a `\\JIVO-APP\...png` path once a QR exists.

---

## 4. Multi-company routing (OIL / BEVERAGE / MART)

An IRN must be generated against — and mirrored into — **the same company DB the
invoice lives in**. `DocEntry` is a *per-company* sequence, so the same number
exists in all three DBs and means a different invoice in each: getting the company
wrong silently reads/writes the wrong document.

**One value drives everything: `company_db`.** It selects the Service Layer login,
the `OMS_IRN_LOG` schema, and the QR folder.

| Setting | Value |
|---|---|
| `HANA_OIL_COMPANY_DB` | `JIVO_OIL_HANADB` |
| `HANA_BEVERAGE_COMPANY_DB` | `JIVO_BEVERAGES_HANADB` |
| `HANA_MART_COMPANY_DB` | `JIVO_MART_HANADB` |

### Auto-IRN (invoice created in OMS)
`SAPInvoiceCreateView` already receives `?branch=` and uses it for the SAP login; it
now resolves the same branch to a company DB and passes it to the IRN:

```python
_maybe_auto_irn(DocEntry, trigger='invoice_create',
                company_db=SAPServiceLayerManager.schema_for(branch), ...)
```

`schema_for()` accepts `BEVERAGE` **or** `BEVERAGES` and falls back to **OIL** for a
missing/unknown branch — so a branch the frontend forgets to send silently means OIL.

### Auto-IRN (polling sweep)
`python manage.py auto_generate_irns` with **no `--company-db` sweeps every configured
company** (previously it only ever did OIL). One scheduled job covers all.

### Manual generation — user picks the company
- `GET /api/einvoice/companies/` → `[{label: OIL, company_db: …}, {label: BEVERAGE, …}]` + default.
- The shared `CompanyDbSelect` component (Invoice Browser, Generate IRN, e-Way Bill)
  populates from that endpoint instead of a hardcoded list, and the chosen
  `company_db` is passed to `listInvoices` / `previewFromInvoice` / `generateFromInvoice`.

### ⚠️ Session cache is per company
`SAPServiceLayerManager` caches the Service Layer session under
`b1_session::<company_db>` / `route_id::<company_db>`. It previously used a **single
global key**, which handed a cached OIL session to a BEVERAGE caller — every beverage
invoice would have been read from and written to OIL regardless of branch. If you
touch that class, keep the key per-company. `clear_session(branch=None)` clears one
company or all.

---

## 5. The Crystal service (`CrystalReportService`)

- ASP.NET Web API 2 (.NET 4.8). Endpoints:
  - `GET /api/billprint/{docEntry}` → renders `Reports/BillPrint.rpt`, returns PDF.
  - `GET /api/health` → `{"status":"ok"}`.
- `Web.config` appSettings: `DbServer=HANA_LIVE_OIL`, `DbName=JIVO_OIL_HANADB`,
  `DbUser=DSR`, `DbPassword=…`, `ReportParamName=DocKey@`.
- IIS: dedicated app pool **CrystalReportService**, No-Managed-Code? No — CLR v4,
  Integrated, **Enable 32-bit = False** (must match 64-bit Crystal runtime).
- The report's QR is a **picture object** whose **Graphic Location** formula returns
  the `UNE QR Code` field (`Format Graphic → Picture tab → Graphic Location → x+2`).
  A plain text/field placement will *never* render as an image.

> ⚠️ **The render service is bound to ONE company.** `Web.config` pins
> `DbServer=HANA_LIVE_OIL` / `DbName=JIVO_OIL_HANADB`, so `/api/billprint/{DocEntry}`
> always resolves the DocEntry **in OIL** — even though the SP is now deployed in all
> three company schemas (§3) and the IRN/QR data is per-company (§4). Printing a
> BEVERAGE or MART invoice through this endpoint would read the *wrong* document
> (DocEntry is a per-company sequence). Supporting multi-company printing needs the
> service to select the DSN/company per request (e.g. a `?company=` parameter mapped
> to per-company ODBC DSNs) — **not yet done**.

---

## 6. THE FIX — QR blank when printed from OMS (but fine in Crystal Designer)

**Symptom:** invoice prints from OMS with all data but a **blank QR box**; opening
the same report in Crystal Designer shows the QR. Sometimes a hard **HTTP 500**.

**Root cause:** Crystal opens the QR UNC at render time using the **IIS worker's
Windows identity**. The app pool ran as `ApplicationPoolIdentity` = the machine
account on the network, which has **no rights** to the auth-protected
`\\JIVO-APP` shares → image load fails (blank, or 500). Designer works because the
interactive login *does* have share access.

**Fix applied (works):** run the app pool as a real local user that mirrors the
share account (workgroup mirrored-account SMB auth). On **.75**:

```powershell
# 1. local user matching JIVO-APP's OMS account (same name + password!)
New-LocalUser -Name OMS -Password (ConvertTo-SecureString 'Jivo@2026' -AsPlainText -Force) `
  -FullName 'OMS Crystal Share' -Description 'IIS Crystal share access' `
  -PasswordNeverExpires -UserMayNotChangePassword

# 2. rights the IIS worker needs
Add-LocalGroupMember -Group IIS_IUSRS -Member OMS
#    + grant "Log on as a batch job" (SeBatchLogonRight) to OMS  (secedit)
icacls "C:\Windows\Temp" /grant "OMS:(OI)(CI)M"

# 3. point the app pool at it, recycle
Import-Module WebAdministration
Set-ItemProperty 'IIS:\AppPools\CrystalReportService' processModel.identityType 3   # SpecificUser
Set-ItemProperty 'IIS:\AppPools\CrystalReportService' processModel.userName '.\OMS'
Set-ItemProperty 'IIS:\AppPools\CrystalReportService' processModel.password 'Jivo@2026'
Restart-WebAppPool CrystalReportService
```

Because `.75\OMS` and `JIVO-APP\OMS` share the same username+password, the render
worker authenticates to `\\JIVO-APP\Jivo Oil\Bitmaps` (and `\OMS_Attachments\Bitmap`)
transparently. Verified: QR embeds (250×250) in the PDF.

**Why not `cmdkey`:** storing a credential in the `IIS AppPool\CrystalReportService`
virtual account is **not automatable** — Windows won't run a command as that
virtual account via `schtasks`/`Register-ScheduledTask` (ERROR_NONE_MAPPED /
task-never-runs). The mirrored local-user identity above is the reliable route.

**If the share password changes:** reset the local user to match —
`net user OMS <newpass>` — and update `processModel.password` on the app pool.

**Rollback:** set the app pool identity back to `ApplicationPoolIdentity` and
`Remove-LocalUser OMS`.

---

## 7. Troubleshooting decision tree

| Symptom | Likely cause | Action |
|---|---|---|
| Blank QR box, rest of invoice fine | Render identity can't read the QR share | §6 fix; verify `\\JIVO-APP\Jivo Oil\Bitmaps\<hash>.png` opens as the app-pool user |
| IRN/Ack/AckDate blank but the IRN exists | Old SP still QR-gates those fields | Redeploy the SP (§3, `CREATE OR REPLACE`) — the QR condition was removed 2026-07-29 |
| "e-invoice not generated" label shows despite an IRN | Old `"Blank for Billing"` (QR-gated) | Same redeploy (§3). If it persists, the label is a formula **inside the `.rpt`** reading the QR field — fix it in Designer |
| IRN written to the wrong company's `OMS_IRN_LOG` | `company_db`/branch not passed, or stale global session cache | §4 — check the branch the frontend sends; confirm per-company cache keys |
| QR saved but Mart QRs missing | Folder spelled `MART_ATACHMENTS` (one T) | Must be `MART_ATTACHMENTS`; re-run `test_qr_share` on .75 |
| `OMS_IRN_LOG.U_UTL_QRPT` is NULL | QR-save hook didn't run/failed (it's best-effort, only logs) | Check `EINV_QR_SAVE_DIRS`, SMB creds, and that the app host can resolve `JIVO-APP`; `test_qr_share` |
| HTTP 500 from `/api/billprint/{de}` | Crystal throws on inaccessible dynamic image; OR report/param/DB error | Check IIS log `C:\inetpub\logs\LogFiles\W3SVC*\` (sc-status 500 vs 200); recycle pool; check `ReportParamName=DocKey@`; check ODBC DSN `HANA_LIVE_OIL` |
| QR shows a placeholder icon (e.g. an arrow) | Graphic Location formula missing/points to wrong field; showing the inserted static image | In Designer set the picture's Graphic Location = `{…UNE QR Code}`, re-publish the `.rpt` to `C:\inetpub\...\Reports\` |
| Wrong/duplicate IRN or QR for dual-source invoice | Priority in the SP | It's `@UTL_MDEXTH` first, `OMS_IRN_LOG` fallback — adjust `SRC_PRIO` in `oms_sp_gst_invoice.sql` if needed |
| No QR data at all for an invoice | Not in either table with `U_UTL_IST='S'` and non-blank `U_UTL_QRPT` | Confirm IRN was generated; check `OMS_IRN_LOG` / `@UTL_MDEXTH` for that `BaseEntry`, `DocType=13` |
| Works from Designer, not from OMS | Identity difference (interactive login vs app-pool) | §6 |
| Data correct in `CALL` but not in report | `.rpt` bound to a different proc/alias, or served copy stale | Confirm the `.rpt` command maps to `OMS_SP_GST_INVOICE`; re-copy the `.rpt` to `C:\inetpub\...\Reports\` |

Fast checks:
```powershell
# is the render service up + does it render (from .75)?
Invoke-WebRequest http://localhost:8008/api/health
Invoke-WebRequest http://localhost:8008/api/billprint/54110 -OutFile C:\Windows\Temp\t.pdf

# can the app-pool user reach the shares? (test with the OMS creds, from .75)
net use "\\JIVO-APP\Jivo Oil" /user:OMS Jivo@2026
Test-Path "\\JIVO-APP\Jivo Oil\Bitmaps\<hash>.png"
net use "\\JIVO-APP\Jivo Oil" /delete /y

# all three OMS QR folders, end to end (write + read back + delete)
python manage.py test_qr_share
```
`JIVO-APP` must resolve to `20.20.45.25` from the app host — it does from **.75**,
but **not** from a typical dev laptop, so QR-share checks only mean anything on .75.
To confirm the QR is actually *in* the PDF (not just a 200): open the PDF and look,
or extract image XObjects — a valid QR is a square image (~166–250 px). A blank
white square = the image didn't load.

---

## 8. Related

- API-level IRN/EWB integration, schema, error codes, crypto:
  [NIC_EINVOICE_EWAYBILL_REFERENCE.md](./NIC_EINVOICE_EWAYBILL_REFERENCE.md)
- Stored proc source (OMS report): [`../sql/oms_sp_gst_invoice.sql`](../sql/oms_sp_gst_invoice.sql)
- Stored proc source (SAP-run `.25` report): [`../sql/oms_sp_gst_invoice_sap.sql`](../sql/oms_sp_gst_invoice_sap.sql)
  — same dual-source UNION but **no path rewrite** (raw stored paths), for the report
  that runs inside SAP B1. It still has the old QR-gated IRN/Ack subqueries.
- QR write path: `einvoice/services.py` — `qr_dir_for_company()`, `save_qr_to_dir()`,
  `_save_png_smb()`; settings `EINV_QR_SAVE_DIRS`, `EINV_QR_SMB_*`, `EINV_QR_FILENAME`
- Company routing: `serviceLayer/service.py` (`schema_for`, `branch_for`, per-company
  session cache), `serviceLayer/views.py` (auto-IRN wiring), `einvoice/sap.py`
  (`company_choices()`, `get_session()`)
- Share checks: `python manage.py test_qr_share` (run on .75)
- Crystal service source: `C:\LiveProjects\Crystal Report Utility\` (README inside)
