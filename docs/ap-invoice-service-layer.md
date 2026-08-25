# AP Invoice via SAP Service Layer (copy from GRPO)

How to post an A/P invoice for RM/PM by copying from a Goods Receipt PO, and the
attachment problem that currently blocks the document-upload half of the flow.

Verified end-to-end against `TEST_JIVO_OIL_HANADB` on 2026-08-24.
Service Layer: `https://138.252.101.222:50000/b1s/v2`.

---

## 1. What an AP invoice is here

The GRPO records that goods physically arrived: `Dr Stock / Cr GRNI`. The A/P
invoice records that the vendor has billed us: `Dr GRNI + Dr Input CGST/SGST /
Cr Sundry Creditor`. It clears the GRNI holding account, creates the payable, and
claims the input tax credit. Posting it closes the GRPO.

That is why it is a *copy* of the GRPO rather than fresh data entry — the
quantities and values must agree with what was received, and only the vendor's
own reference and any tax/TDS treatment are genuinely new.

## 2. Minimum payload

`POST /PurchaseInvoices`

```json
{
  "CardCode": "VENDA001320",
  "NumAtCard": "vendor's own invoice number",
  "AttachmentEntry": 170747,
  "DocumentLines": [
    {"BaseType": 20, "BaseEntry": 25773, "BaseLine": 1},
    {"BaseType": 20, "BaseEntry": 25773, "BaseLine": 2}
  ]
}
```

Everything else is copied by SAP: item code, quantity, unit price, warehouse,
tax code, line totals, document total, due date, journal memo.

| Field | Meaning |
|---|---|
| `BaseType` | `20` = GRPO (`22` = Purchase Order, `18` = AP Invoice) |
| `BaseEntry` | The GRPO's **`DocEntry`** — the internal key, *not* `DocNum` |
| `BaseLine` | The GRPO line's **`LineNum`** |
| `NumAtCard` | Vendor's invoice number. Need **not** be numeric |
| `AttachmentEntry` | The GRPO's own attachment id — see §4 |

### `BaseLine` is not a sequence

`LineNum` values are not contiguous and do not restart. GRPO 25773's open lines
were `LineNum` **1 and 2** (line 0 was already closed) and they became `LineNum`
0 and 1 on the resulting invoice. Always read the actual values and copy only
lines whose `LineStatus == "bost_Open"`; never assume `0..n`.

### Verified result

GRPO 25773 → AP invoice **DocEntry 49565 / DocNum 626084130**:

```
PM0000008  qty 696    @ 23.0  IGST@5  wh BH-PM  lineTotal 16008.0
PM0000008  qty 2079   @ 23.0  IGST@5  wh BH-PM  lineTotal 47817.0
DocTotal 67016.0   VatSum 3191.25    (identical to the GRPO)
```

The GRPO moved to `bost_Close`, both lines to `bost_Close`.

## 3. What can and cannot be overridden

- **Overridable:** `Quantity`, `UnitPrice` (for short/over-billing), `DocDate`,
  `DocDueDate`, `NumAtCard`, `WTLiable`, `Comments`.
- **Rejected on a copied line:** `TaxCode`, `WarehouseCode` — they come from the
  base document and must not be sent.
- `CardCode` is the one addressing field that matters; the rest of the
  bill-to/ship-to block is resolved from the business partner.

### TDS / withholding

India localisation, via `WithholdingTaxDataCollection`:

```json
{"WTLiable": "tYES",
 "WithholdingTaxDataCollection": [{"WTCode": "1031"}]}
```

`1031` is s.194Q, 0.1% above a ₹50,00,000 threshold. The code **must be in the
vendor's own allowed list** (`OCRD` → `WTX1`), otherwise SAP rejects with
`1250000075`. Read the vendor's permitted codes and present only those. Let SAP
compute `WTAmount`; do not send it.

## 4. The attachment problem (`-5002`)

Posting a copy of a GRPO that *has* an attachment fails:

```
-5002  Attachments folder not defined, or Attachments folder has been changed or removed
       [Message 131-102]
```

`CopyAttachmentsFromBaseToTarget` is `tYES`, so SAP tries to copy the GRPO's
attachment to the new invoice and cannot.

### The workaround that works

Pass the GRPO's own `AttachmentEntry` in the payload. SAP then **links** the
existing attachment instead of copying it, and the post succeeds. Side effect:
the GRPO and the AP invoice share one attachment row, so the invoice's
"attachment" is the GRPO's document, not the vendor's invoice PDF.

### Root cause — a flapping CIFS mount, NOT config or permissions

`.222` is the Linux HANA/B1 server (SLES 15-SP5, host `hanadb`). The `.52` file
shares are CIFS-mounted there:

```
//10.10.101.52/Attachments_Oil  ->  /mnt/Attachments_Oil   (uid=465,gid=464,mode=0770,soft,...)
```

Service Layer runs as OS user **`b1service0` (uid 465 / gid 464)** — the exact
owner of that mount. Proven by direct test on 2026-08-25:

- `b1service0` can **read** `/mnt/Attachments_Oil/JIVO_OIL/Attachments/` (full of
  real PDFs, owner b1service0 rwx) and **write** it (create+delete temp file).
- Then `POST /Attachments2` was fired **20×** against TEST_JIVO_OIL — **all HTTP
  201**, and `/proc/fs/cifs/Stats` for the share showed **+20 Writes** (the files
  really landed). Service Layer attachment upload WORKS.

So it is **not** permissions, **not** a UNC→mount config gap, **not** the OS
identity. `/proc/fs/cifs/DebugData` showed **`Instance: 5`** — the SMB session to
`.52` had re-established 5 times; the mount is **flapping**. Because it is mounted
`soft`, every access during a disconnect returns an error, and Service Layer
reports that as `-5002` "folder ... changed or removed." Running the diagnostic
access above forced a reconnect and healed it — for SL too.

**Therefore `-5002` here is INTERMITTENT**, tied to `.222`↔`.52` SMB connectivity
(likely destabilised by the `.25`→`.52` IP migration). It returns whenever the
CIFS session drops and clears once it reconnects.

**Remedies (in order):**

1. Stabilise the `.222`↔`.52` SMB connection — find why it flaps (network, `.52`
   reboots/idle timeout). Consider a keepalive or an auto-remount watchdog.
2. Make OMS treat `-5002` as **retryable** — retry the call, or fall back to the
   file-upload utility route (below).

**Do NOT** (all verified as wrong turns):

- add `b1service0` to group 464 — it already owns the mount;
- change the configured path — it is correct, and `CompanyService_UpdatePathAdmin`
  is a silent no-op (HTTP 204, value unchanged) anyway; the path is only settable
  in the B1 client;
- restart Service Layer — it does not fix connectivity and disrupts OMS + the web/
  mobile clients (the B1 Windows desktop client is unaffected — it uses DI/direct
  HANA, not Service Layer);
- treat `AdminInfo.AttachmentEntryForFileStorage` as the path — it is `Edm.Int32`,
  unrelated; earlier work misread it as `OADM.AttachPath` and chased a dead end.

### The resilient way regardless

The file-upload utility runs on the `.118` server and reaches `.52` over its own
connection; its **folder id 3** is that exact attachment folder. See
[file-upload-utility.md](./file-upload-utility.md). Uploading the vendor document
there puts it where SAP expects attachments even if SL's own session is briefly
down.

Note: Service Layer **blocks `DELETE` on `Attachments2`** (error 220), so an
attachment row created in error cannot be removed through the API — only the
physical file can be cleaned up, leaving the DB row orphaned.

## 5. OMS implementation

Backend — `serviceLayer/ap_views.py`, routed in `serviceLayer/urls.py`:

| Endpoint | Purpose |
|---|---|
| `GET /api/service-layer/ap/open-grpos/?branch=OIL` | List open GRPOs (optional `vendor`, `search`) |
| `GET /api/service-layer/ap/grpo/?branch=OIL&doc_num=` | One GRPO + its **open lines only** (also accepts `doc_entry`) |
| `GET /api/service-layer/ap/vendor-tds/?branch=OIL&card_code=` | Vendor WT flag + active TDS code master |
| `POST /api/service-layer/ap/invoice/?branch=OIL` | Create the A/P invoice |

The POST takes **friendly fields, not a raw SAP payload** (unlike the older
`SAPInvoiceCreateView`), and builds the SAP body server-side — so the client can
never send a malformed `BaseType`/`BaseEntry`. Body:

```json
{"grpo_entry": 25771, "num_at_card": "INV/2026/01",
 "doc_date": "2026-08-25", "due_date": "2026-09-15", "comments": "…",
 "attachment_entry": 170738,
 "tds": {"liable": true, "wt_code": "TDS"},
 "lines": [{"base_line": 0, "quantity": 42, "unit_price": 5.0}]}
```

`quantity`/`unit_price` are **optional per line** — omit them and SAP copies the
GRPO values. The frontend only sends them when the user actually changed the
value. Posts as `SAP_APPROVER_USER` so SAP doesn't intercept into a draft.

Frontend — `pages/Ap_Invoice_Entry.tsx`, `services/apInvoiceService.ts`,
`styles/Ap_Invoice_Entry.css`, route `/Ap_Invoice_Entry` in `App.tsx`.

### Access — the `tracker_ap` role

AP entry is a tracker sub-role, so tracker admins manage these users with the
same screen they already use (Tracker Config → Users).

| Role | Sees AP Invoice Entry |
|---|---|
| `tracker_ap` | Yes — and nothing else; AP entry is their whole job |
| `tracker_admin` | Yes, plus every tracker page (and manages AP users) |
| `tracker_entry` / `tracker_user` | No |

Defined once per side and mirrored: `ROLE_PAGE_MAP` in `tracker/permissions.py`
(page key `Ap_Invoice_Entry`, enforced by `IsTrackerAP` on all four endpoints)
and `TRACKER_ROLE_PAGES` in `src/config/pageAccess.ts` (drives the sidebar link,
the in-page guard and the post-login landing page). `TRACKER_ROLE_NAMES` derives
from `ROLE_PAGE_MAP`, so the tracker-admin user management picked the role up
automatically.

The role row itself is seeded by migration `tracker/0020_seed_tracker_ap_role`
(roles are `users.UserRole` rows, so it must exist in every environment). Its
reverse refuses to delete the role while any user still holds it.

Note `Ap_Invoice_Entry` is also in the `LANDING_ORDER` list — it is the only page
a `tracker_ap` user can open, so omitting it would land them on a forbidden page
after login.

**Editable vs copied** (the core UX rule — `.ap-edit` marks every editable
control white, copied fields are grey and inert):

| Editable by the user | Copied from the GRPO (read-only) |
|---|---|
| Vendor Invoice No. (`NumAtCard`) — required | Vendor (`CardCode`/name) |
| Invoice Date, Payment Due Date | Item code & description |
| Remarks (`Comments`) | Warehouse, Tax code, UoM |
| TDS on/off + TDS code | Line totals, document total, VAT |
| Per-line **Quantity** and **Unit Price** | Which GRPO / `BaseEntry` / `BaseLine` |
| Whether to attach the GRPO's document | |

Verified against `TEST_JIVO_OIL_HANADB` on 2026-08-25: the payload this code
builds posted as DocEntry 49574 / DocNum 626084132 (total 253,110 = the GRPO's),
with DocDate/DocDueDate/Comments/NumAtCard all applied and the GRPO closed.

## 6. Notes for the OMS frontend

- Users pick a **GRPO**, not lines: fetch it, show the open lines read-only, and
  let them edit only `NumAtCard`, dates, TDS and (where short-billing) qty/price.
- Send `DocEntry`/`LineNum` from the fetched document; never let the UI
  reconstruct them.
- `NumAtCard` should be unique per vendor. Non-numeric values are accepted (1,708
  of 2,323 live AP invoices are non-numeric), so do not validate it as a number.
- Item AP invoices are not strictly GRPO-only in live data — 39 standalone and 38
  intercompany exist — but copy-from-GRPO is the correct path for RM/PM.
- Surface `-5002` as "attachment could not be stored" rather than a raw code; it
  is an infrastructure fault, not user error.

## 7. Test scripts

Working scripts from the verification run (session scratchpad, `ap/`):
`sl.py` (Service Layer client **pinned to TEST**, refuses any DB not prefixed
`TEST_`), `find_rmpm.py`, `ap_flow.py`, `ap_post.py`, `ap_verify.py`,
`ap_attach.py`, `pathadmin_test.py`.
