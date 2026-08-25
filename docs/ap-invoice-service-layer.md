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

## 5. Notes for the OMS frontend

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

## 6. Test scripts

Working scripts from the verification run (session scratchpad, `ap/`):
`sl.py` (Service Layer client **pinned to TEST**, refuses any DB not prefixed
`TEST_`), `find_rmpm.py`, `ap_flow.py`, `ap_post.py`, `ap_verify.py`,
`ap_attach.py`, `pathadmin_test.py`.
