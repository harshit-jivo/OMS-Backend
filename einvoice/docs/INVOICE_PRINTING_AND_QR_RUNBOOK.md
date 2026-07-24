# OMS Invoice Printing, Crystal Report & QR — Operations Runbook

Practical, end-to-end reference for **how a GST tax invoice gets printed from OMS
with its IRN + signed QR**, where every piece lives, and how to fix it when the
QR (or the whole PDF) fails. Complements the API-level
[NIC e-Invoice + e-Way Bill reference](./NIC_EINVOICE_EWAYBILL_REFERENCE.md)
(IRN generation, schema, error codes, crypto).

> Last verified: 2026-07-23. Server/paths/credentials can drift — verify before acting.

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

Two tables can hold an invoice's IRN + QR, per HANA company schema:

| Table | Populated by | QR path it stores |
|---|---|---|
| `@UTL_MDEXTH` | SAP e-invoice **add-on** (invoices done in SAP) | `C:\SAP Attachments\Jivo Oil\Bitmaps\<hash>.png` → served as `\\JIVO-APP\Jivo Oil\Bitmaps\<hash>.png` |
| `OMS_IRN_LOG` | **OMS** e-invoice flow (`einvoice` app) | `\\JIVO-APP\OMS_Attachments\Bitmap\<hash>.png` |

- Both store the **same** signed-QR image content per invoice (same hash filename),
  just under different shares.
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
  - `@UTL_MDEXTH`: `REPLACE("U_UTL_QRPT",'C:\SAP Attachments\','\\JIVO-APP\')`
    → `\\JIVO-APP\Jivo Oil\Bitmaps\...`
  - `OMS_IRN_LOG`: left as-is → `\\JIVO-APP\OMS_Attachments\Bitmap\...`
- Output columns preserved incl. `"LICENSE FSSAI"`, `"Customer Fassai No"`,
  `"UNE QR Code"`, `"UNE IRN No"`, `"UNE Ack no"`, `"UNE Ack dt"`.

### Deploy / redeploy the SP
The `.sql` file starts with `DROP PROCEDURE ... IF EXISTS;` then `CREATE PROCEDURE`.
Run it against the target HANA schema with the schema set first (the proc uses
unqualified table names):

```sql
SET SCHEMA "JIVO_OIL_HANADB";
DROP PROCEDURE "OMS_SP_GST_INVOICE";        -- run as a separate statement
CREATE PROCEDURE ... END                    -- the big block, one statement
```

Or from the backend venv (reuses the project's HANA connection):
```
# a small script: connect via hana.services.connection.HANAConnection,
#   cur.execute('SET SCHEMA "JIVO_OIL_HANADB"'); DROP; execute the CREATE block.
```
Quick sanity check: `CALL "OMS_SP_GST_INVOICE"(54110);` — one invoice per line,
`"UNE QR Code"` should be a `\\JIVO-APP\...png` path.

---

## 4. The Crystal service (`CrystalReportService`)

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

---

## 5. THE FIX — QR blank when printed from OMS (but fine in Crystal Designer)

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

## 6. Troubleshooting decision tree

| Symptom | Likely cause | Action |
|---|---|---|
| Blank QR box, rest of invoice fine | Render identity can't read the QR share | §5 fix; verify `\\JIVO-APP\Jivo Oil\Bitmaps\<hash>.png` opens as the app-pool user |
| HTTP 500 from `/api/billprint/{de}` | Crystal throws on inaccessible dynamic image; OR report/param/DB error | Check IIS log `C:\inetpub\logs\LogFiles\W3SVC*\` (sc-status 500 vs 200); recycle pool; check `ReportParamName=DocKey@`; check ODBC DSN `HANA_LIVE_OIL` |
| QR shows a placeholder icon (e.g. an arrow) | Graphic Location formula missing/points to wrong field; showing the inserted static image | In Designer set the picture's Graphic Location = `{…UNE QR Code}`, re-publish the `.rpt` to `C:\inetpub\...\Reports\` |
| Wrong/duplicate IRN or QR for dual-source invoice | Priority in the SP | It's `@UTL_MDEXTH` first, `OMS_IRN_LOG` fallback — adjust `SRC_PRIO` in `oms_sp_gst_invoice.sql` if needed |
| No QR data at all for an invoice | Not in either table with `U_UTL_IST='S'` and non-blank `U_UTL_QRPT` | Confirm IRN was generated; check `OMS_IRN_LOG` / `@UTL_MDEXTH` for that `BaseEntry`, `DocType=13` |
| Works from Designer, not from OMS | Identity difference (interactive login vs app-pool) | §5 |
| Data correct in `CALL` but not in report | `.rpt` bound to a different proc/alias, or served copy stale | Confirm the `.rpt` command maps to `OMS_SP_GST_INVOICE`; re-copy the `.rpt` to `C:\inetpub\...\Reports\` |

Fast checks:
```powershell
# is the render service up + does it render (from .75)?
Invoke-WebRequest http://localhost:8008/api/health
Invoke-WebRequest http://localhost:8008/api/billprint/54110 -OutFile C:\Windows\Temp\t.pdf

# can the app-pool user reach the share? (test with the OMS creds)
net use \\JIVO-APP\IPC$ /user:OMS Jivo@2026
Test-Path "\\JIVO-APP\Jivo Oil\Bitmaps\<hash>.png"
net use \\JIVO-APP\IPC$ /delete /y
```
To confirm the QR is actually *in* the PDF (not just a 200): open the PDF and look,
or extract image XObjects — a valid QR is a square image (~166–250 px). A blank
white square = the image didn't load.

---

## 7. Related

- API-level IRN/EWB integration, schema, error codes, crypto:
  [NIC_EINVOICE_EWAYBILL_REFERENCE.md](./NIC_EINVOICE_EWAYBILL_REFERENCE.md)
- Stored proc source: [`../sql/oms_sp_gst_invoice.sql`](../sql/oms_sp_gst_invoice.sql)
- QR write path: `einvoice/services.py` (`_save_png_smb`, `EINV_QR_SMB_*`)
- Crystal service source: `C:\LiveProjects\Crystal Report Utility\` (README inside)
