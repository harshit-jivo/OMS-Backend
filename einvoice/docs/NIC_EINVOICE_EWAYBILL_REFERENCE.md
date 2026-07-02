# NIC e-Invoice (IRN) + e-Way Bill — Integration Reference

Authoritative working reference for the `einvoice` and `ewaybill` apps in OMS-Backend.
Combines the GSTN-published field mapping + validation regexes with researched
NIC API behaviour. **Always validate exact strings/codes against the live NIC
portal before production**, as NIC revises codes and rules periodically.

Source portals: `einv-apisandbox.nic.in` (sandbox API docs), `einvoice1.gst.gov.in`
(live IRP + error list `/others/geterrorcodes/INV`), `einvoice6.gst.gov.in`
(validation rules), `docs.ewaybillgst.gov.in` (EWB API), CBIC Rule 48 / Rule 138.

---

## 0. The two systems at a glance

| | e-Invoice (IRP) | e-Way Bill (EWB) |
|---|---|---|
| Legal basis | Rule 48(4) CGST Rules | Sec 68 + Rule 138 CGST |
| Purpose | Authenticate a B2B/export invoice → **IRN** + signed QR | Authorise **movement of goods** |
| Trigger | Turnover ≥ ₹5 cr (current), per invoice | Consignment value > ₹50,000 |
| Prod hosts | `api.einvoice1.gst.gov.in`, `api.einvoice2...` | `api.ewaybillgst.gov.in/v1.03` |
| Crypto | RSA(app_key)→SEK→AES-256-ECB | **Same scheme**, separate host/creds |
| Cancel window | 24 h | 24 h |
| Our app | `einvoice/` | `ewaybill/` (reuses `einvoice.crypto`) |

The two are **integrated at NIC**: once an IRN exists, the IRP can generate the
EWB (auto-filling Part A from the registered invoice) via `/eiewb/...` — our
`EInvoiceClient.generate_ewb_by_irn()`. A fully standalone EWB uses the separate
EWB system — our `ewaybill/` app.

---

## 1. e-Invoice — what it is & applicability

- e-Invoicing = **reporting an already-generated invoice** to an Invoice
  Registration Portal (IRP), which returns an **IRN** and a **digitally signed QR**.
  An invoice under Rule 48(4) that is *not* reported is **not a valid invoice** (Rule 48(5)).
- **Turnover threshold history:** ₹500cr (Oct'20) → ₹100cr → ₹50cr → ₹20cr →
  ₹10cr → **₹5cr (1 Aug 2023, current)**. Aggregate turnover in *any* FY since 2017-18.
- **Documents requiring IRN:** `INV` (invoice), `CRN` (credit note), `DBN` (debit
  note) for **B2B, SEZ, exports, deemed exports, reverse-charge**.
  **B2C is excluded.** Exempt sectors: banks/NBFC, insurers, GTA, passenger
  transport, cinema, SEZ *units* (a SEZ unit as supplier cannot generate e-invoices).

## 2. IRN generation flow

1. ERP builds the invoice in the notified JSON schema (INV-01, `Version "1.1"`).
2. Authenticate → get `AuthToken` + `Sek` (cache them).
3. POST encrypted invoice to `/eicore/v1.03/Invoice`.
4. IRP validates structure + business rules → generates IRN, signs invoice, signs QR.
5. Returns `Irn`, `AckNo`, `AckDt`, `SignedInvoice`, `SignedQRCode` (+ `EwbNo`/`EwbDt` if EWB requested).
6. **IRP purges the invoice after ~24 h** → the taxpayer must persist the response.

### IRN computation (deterministic — we can pre-compute & cross-check)
`IRN = SHA-256( SupplierGSTIN + FinancialYear + DocType + DocNo )` → 64-hex chars.
- FinancialYear format `YYYY-YY` (e.g. `2019-20`).
- Example input string: `01AAAAA9999A19N` + `2019-20` + `INV` + `ABC01234`
  → `01AAAAA9999A19N2019-20INVABC01234` → SHA-256.

### Signed invoice & QR
- Both are **JWS** (`header.payload.signature`, SHA256RSA) signed with NIC's key.
- **SignedInvoice** = full invoice JSON as JWS.
- **SignedQRCode** payload = 10 fields: `SellerGstin, BuyerGstin, DocNo, DocTyp,
  DocDt, TotInvVal, ItemCnt, MainHsnCode, Irn, IrnDt`.
- Verify **offline** against NIC's public cert (QR Verifier app) — tampering breaks the signature.
- The signed QR **must be printed** on the invoice.

### Lifecycle
- **Cancel within 24 h** only, whole-invoice only (no partial). Reason codes:
  **1-Duplicate, 2-Data entry mistake, 3-Order cancelled, 4-Other** (+ remarks).
- **No amendment on the IRP** — cancel + regenerate (within 24 h), else amend via GSTR-1.
- **Duplicate** (same GSTIN+FY+Typ+No) → IRP rejects (Error 2150) and returns the *existing* IRN.

## 3. e-Invoice API — crypto, envelope, endpoints, headers

### Auth handshake (`POST /eivital/v1.04/auth`)
1. Generate random **32-byte AppKey** (AES-256 key).
2. Build credentials JSON (`UserName`, `Password`, `AppKey` b64, `ForceRefreshAccessToken`).
   Per NIC ref code, the JSON is **base64-encoded, then RSA-encrypted** (RSA/ECB/PKCS1v1.5)
   with the GST public key → `{"Data": <b64>}`. *(This base64-before-RSA step is exactly
   what `einvoice/client.py._authenticate()` does — skipping it yields generic Auth error 5001.)*
3. Response `Data` → **SEK** (AES-256 session key) encrypted with our AppKey (AES-256-ECB
   PKCS7). Decrypt **once** to recover the raw SEK + `AuthToken` + `TokenExpiry`.
4. **Token valid 6 h.** Re-auth in the window returns the *same* token (clock not reset);
   force a new one with `ForceRefreshAccessToken:"true"`.

### Signed request/response envelope
- Request: `{"Data": "<b64 AES-256-ECB(SEK) of JSON payload>"}`.
- Success: `{"Status":"1","Data":"<b64 AES>","ErrorDetails":null,"InfoDtls":...}`.
- Failure: `{"Status":"0","Data":null,"ErrorDetails":"<b64 JSON [{ErrorCode,ErrorMessage}]>"}`.
- Our client decodes ErrorDetails from base64 → JSON (`_decode_error_details`).

### Endpoints (post-migration: Master/auth = v1.04; eiCore/eiEWB = v1.03)
| Function | Method | Path |
|---|---|---|
| Authenticate | POST | `/eivital/v1.04/auth` |
| Generate IRN | POST | `/eicore/v1.03/Invoice` |
| Cancel IRN | POST | `/eicore/v1.03/Invoice/Cancel` |
| Get IRN by IRN | GET | `/eicore/v1.03/Invoice/irn/{irn}` |
| Get IRN by doc | GET | `/eicore/v1.03/Invoice/irnbydocdetails?doctype=&docnum=&docdate=` |
| Get GSTIN details | GET | `/eivital/v1.04/Master/gstin/{gstin}` |
| Sync GSTIN | GET | `/eivital/v1.04/Master/syncgstin/{gstin}` |
| Generate EWB by IRN | POST | `/eiewb/v1.03/ewaybill` |
| Get EWB by IRN | GET | `/eiewb/v1.03/ewaybill/irn/{irn}` |
| Heartbeat | GET | `/eivital/v1.04/heartbeat/ping` |

### Headers
Business APIs: `client_id`, `client_secret`, `Gstin`, `user_name`, `AuthToken`,
`Content-Type: application/json`. Auth API needs only `client_id` + `client_secret`. TLS 1.2+.

### Common IRN error codes (verify against `/others/geterrorcodes/INV`)
| Code | Meaning |
|---|---|
| 1002 | Invalid login / credentials |
| 1005 | Invalid / expired AuthToken |
| 2150 | **Duplicate IRN** (same GSTIN+Typ+No+FY) — returns existing IRN |
| 2172 | Intra-state: IGST not applicable, only CGST+SGST |
| 2174 | Inter-state: CGST/SGST must be 0 |
| 2176 | HSN code invalid |
| 2182–2187 | Invoice totals (AssVal/CGST/SGST/IGST/Cess) ≠ Σ items |
| 2189 | Total invoice value mismatch |
| 2193 | Item AssAmt ≠ (TotAmt − Discount) |
| 2194 | Item TotItemVal mismatch |
| 2211 | Supplier & recipient GSTIN must not be same |
| 2212 | Recipient GSTIN cannot be URP for this SupTyp |
| 2227 | Intra-state: CgstAmt must equal SgstAmt |
| 2258 / 2265 | Seller / Buyer GSTIN first 2 digits ≠ Stcd |
| 2294 | IgstOnIntra=Y requires reverse charge |
| 3028 | GSTIN invalid / not registered |
| 3029 | GSTIN inactive / cancelled (→ Sync GSTIN, resubmit) |

---

## 4. e-Invoice schema (v1.1) — blocks & business validations

### Top-level blocks
Mandatory: **`Version`, `TranDtls`, `DocDtls`, `SellerDtls`, `BuyerDtls`, `ItemList`
(1–1000), `ValDtls`**. Optional: `DispDtls`, `ShipDtls`, `PayDtls`, `RefDtls`,
`AddlDocDtls`, `ExpDtls`, `EwbDtls`. Payload ≤ 2 MB.

### TranDtls
- `TaxSch` = `"GST"` (fixed).
- `SupTyp`: **B2B, SEZWP, SEZWOP, EXPWP, EXPWOP, DEXP**
  (WP = with IGST payment; WOP = without payment under LUT/bond; DEXP = deemed export).
- `RegRev` Y/N (reverse charge; B2B/SEZ only). `IgstOnIntra` Y/N (Y ⇒ RegRev mandatory).

### DocDtls
- `Typ`: `INV` / `CRN` / `DBN`.
- `No`: ≤16 chars, `^([a-zA-Z1-9]{1}[a-zA-Z0-9/-]{0,15})$` (no leading `0`/`/`/`-`).
- `Dt`: `dd/mm/yyyy`, not future.

### Arithmetic validations (IRP recomputes; **±1 rupee tolerance**)
Per item:
- `TotAmt = Qty × UnitPrice` (gross, pre-discount).
- **`AssAmt = TotAmt − Discount`** (taxable value) — else **2193**.
- Intra-state: `CgstAmt = SgstAmt = AssAmt × GstRt/2`, IGST = 0 (`CgstAmt==SgstAmt` or **2227**).
- Inter-state: `IgstAmt = AssAmt × GstRt`, CGST/SGST = 0.
- `CesAmt = AssAmt × CesRt`.
- **`TotItemVal = AssAmt + CgstAmt + SgstAmt + IgstAmt + CesAmt + StateCesAmt + OthChrg`** — else **2194**.
  *(CRN/DBN item taxes are not rate-cross-checked.)*

Invoice totals (`ValDtls`):
- `AssVal = ΣAssAmt`; `CgstVal/SgstVal/IgstVal/CesVal/StCesVal = Σ respective`.
- **`TotInvVal = AssVal + CgstVal + SgstVal + IgstVal + CesVal + StCesVal + OthChrg − Discount + RndOffAmt`**.
- `RndOffAmt` ∈ [−99.99, +99.99].

### Tax determination
- **POS = Supplier `Stcd` → intra-state → CGST+SGST**; POS ≠ Stcd → inter-state → IGST.
- Exception: `IgstOnIntra=Y` → IGST even intra-state.
- **SEZ/Export → always IGST** regardless of state.
- Export recipient: `Gstin="URP"`, `Pos="96"`, `Pin=999999`.

### GSTIN ↔ state
First 2 digits of Seller `Gstin` = Seller `Stcd` (2258); same for Buyer (2265); exempt for URP/export.

### Integrator gotchas
- Don't confuse **`TotAmt` (gross) vs `AssAmt` (taxable) vs `TotItemVal` (incl. tax)** — #1 cause of 2193/2194.
- Discount subtracted **once** at line level; don't also net it in `TotItemVal`.
- **No negative values** — use CRN, not negatives.
- String fields disallow `"` and `\`; omit empty optionals rather than sending blank.
- 1000-item / 2 MB caps.

---

## 5. GSTN notified-schema → API field mapping (canonical, GSTN portal)

`ListName.ObjectName.AttributeName` on the right is the API JSON path.

**Basic:** Version→`Version` · IRN→`Irn` · Supply_Type→`TranDtls.SupTyp` ·
Doc_Type→`DocDtls.Typ` · Doc_Num→`DocDtls.No` · Doc_Date→`DocDtls.Dt` ·
Addl_Currency→`ExpDtls.ForCur` · Reverse_Charge→`TranDtls.RegRev` ·
IGST_on_Intra→`TranDtls.IgstOnIntra`

**Invoice period:** Start→`RefDtls.DocPerdDtls.InvStDt` · End→`RefDtls.DocPerdDtls.InvEndDt`

**Preceding/Contract:** Prec_Doc_Num→`RefDtls.PrecDoc.InvNo` · Prec_Doc_Date→`RefDtls.PrecDoc.InvDt` ·
Other_Ref→`RefDtls.PrecDoc.OthRefNo` · Rcpt_Advice_Ref→`RefDtls.Contract.RecAdvRefr` ·
Rcpt_Advice_Date→`RefDtls.Contract.RecAdvDt` · Tender/Lot→`RefDtls.Contract.TendRefr` ·
Contract_Ref→`RefDtls.Contract.TendRefr` · External_Ref→`RefDtls.Contract.ExtRefr` ·
Project_Ref→`RefDtls.Contract.ProjRefr` · PO_Ref_Num→`RefDtls.Contract.PORefr` · PO_Ref_Date→`RefDtls.Contract.PORefDt`

**Supplier (`SellerDtls`):** Legal_Name→`LglNm` · Trade_Name→`TrdNm` · GSTIN→`Gstin` ·
Address1→`Addr1` · Address2→`Addr2` · Place→`Loc` · State_Code→`Stcd` · Pincode→`Pin` ·
Phone→`Ph` · Email→`Em`

**Recipient (`BuyerDtls`):** Legal_Name→`LglNm` · Trade_Name→`TrdNm` · GSTIN→`Gstin` ·
Place_Of_Supply→`POS` · Address1→`Addr1` · Address2→`Addr2` · Place→`Loc` · State_Code→`Stcd` ·
Pincode→`Pin` · Country_of_Export→`ExpDtls.CntCode` · Phone→`Ph` · Email→`Em`

**Payee (`PayDtls`):** Payee_Name→`Nam` · Bank_Acct→`AccDet` · Mode→`Mode` · Branch→`FinInBr` ·
Terms→`PayTerm` · Instruction→`PayInstr` · Credit_Transfer→`CrTrn` · Direct_Debit→`DirDr` ·
Credit_Days→`CrDay` · Paid_Amount→`PaidAmt` · Amount_due→`PaymtDue`

**Extra:** Tax_Scheme→`TranDtls.TaxSch` · Remarks→`RefDtls.InvRm` · Port→`ExpDtls.Port` ·
Shipping_Bill_No→`ExpDtls.ShipBNo` · Shipping_Bill_Date→`ExpDtls.ShipBDt` ·
Export_Duty→`ExpDtls.ExpDuty` · Refund_Claim→`ExpDtls.RefClm` · ECOM_GSTIN→`TranDtls.EcmGstin`

**Addl docs (`AddlDocDtls`):** URL→`Url` · base64→`Docs` · Info→`Info`

**E-way bill (`EwbDtls`):** Transporter_ID→`TransId` · Trans_Mode→`TransMode` · Distance→`Distance` ·
Transporter_Name→`TransName` · Trans_Doc_No→`TrnDocNo` · Trans_Doc_Date→`TrnDocDt` ·
Vehicle_No→`VehNo` · Vehicle_Type→`VehType`

**Ship-to (`ShipDtls`):** Legal_Name→`LglNm` · Trade_Name→`TrdNm` · GSTIN→`Gstin` ·
Address1→`Addr1` · Address2→`Addr2` · Place→`Loc` · Pincode→`Pin` · State_Code→`Stcd`

**Dispatch-from (`DispDtls`):** Name→`Nm` · Address1→`Addr1` · Address2→`Addr2` · Place→`Loc` ·
State_Code→`Stcd` · Pincode→`Pin`

**Item (`ItemList[].`):** Sl_No→`SlNo` · Description→`PrdDesc` · Is_Service→`IsServc` · HSN→`HsnCd` ·
Barcode→`BarCd` · Quantity→`Qty` · Free_Qty→`FreeQty` · Unit→`Unit` · Price→`UnitPrice` ·
Gross_Amount→`TotAmt` · Discount→`Discount` · Pre_Tax_Value→`PreTaxVal` · Taxable_Value→`AssAmt` ·
GST_Rate→`GstRt` · IGST→`IgstAmt` · CGST→`CgstAmt` · SGST/UTGST→`SgstAmt` ·
Cess_Rate→`CesRt` · Cess_Amt→`CessAmt` · Cess_NonAdVal→`CesNonAdvlAmt` ·
State_Cess_Rate→`StateCesRt` · State_Cess_Amt→`StateCesAmt` · State_Cess_NonAdVal→`StateCesNonAdvlAmt` ·
Other_Charges→`OthChrg` · PO_Line_Ref→`OrdLineRef` · Item_Total→`TotItemVal` ·
Origin_Country→`OrgCnt` · Serial_No→`PrdSlNo`
Batch: Batch_No→`BchDtls.Nm` · Expiry→`BchDtls.ExpDt` · Warranty→`BchDtls.WrDt`
Attributes: Name→`Attribute.Nm` · Value→`Attribute.Val`

**Document totals (`ValDtls`):** Taxable_Total→`AssVal` · IGST_Total→`IgstVal` · CGST_Total→`CgstVal` ·
SGST_Total→`SgstVal` · Cess_Total→`CesVal` · State_Cess_Total→`StCesVal` · Discount_Invoice→`Discount` ·
Other_Charges_Invoice→`OthChrg` · Round_off→`RndOffAmt` · Total_INR→`TotInvVal` · Total_FCNR→`TotInvValFc`

## 6. GSTN validation regexes (implement client-side, pre-submit)

| Field | Regex |
|---|---|
| Document_Num | `^([a-zA-Z1-9]{1}[a-zA-Z0-9/-]{0,15})$` |
| Document_Date / any dd/mm/yyyy (2010s–2020s) | `^[0-3][0-9]/[0-1][0-9]/[2][0][1-2][0-9]$` |
| Period / Batch / Warranty date (1980–2059) | `^(0[1-9]\|[12][0-9]\|3[01])/(0[1-9]\|1[012])/(19[8-9][0-9]\|20[0-5][0-9])$` |
| GSTIN (Supplier/ECOM/TransporterID) | `([0-9]{2}[A-Z 0-9]{13})` |
| GSTIN (Recipient/ShipTo) | `([0-9]{2}[A-Z 0-9]{13})\|URP` |
| State code / POS | `^([0-9]{1,2})$` |
| Phone | `^([0-9]{6,12})$` |
| Email | `^[a-zA-Z0-9+_.-]+@[a-zA-Z0-9.-]+$` |
| Trans_Mode | `([1-4]{1})?` |
| Trans_Doc_No | `^[a-zA-Z0-9]{1}[a-zA-Z0-9-/]*$` |
| Sl_No | `^[0-9]*$` |
| HSN_Code | `^[0-9]*$` (min 6 digits; min 4 if turnover < ₹5cr) |
| Qty / Free_Qty / Price / rates (0–3 dp) | `^\d+.?\d{0,3}$` |
| Amounts (0–2 dp) | `^\d+.?\d{0,2}$` |
| Round_off_amount (signed, 0–2 dp) | `^-?\d+.?\d{0,2}$` |

---

## 7. e-Way Bill system

- **Legal:** Sec 68 + Rule 138. Required before movement when **consignment value > ₹50,000**
  (single or aggregate in one conveyance). For supply, non-supply (branch transfer/returns),
  or inward from unregistered. Penalty: ₹10,000 or tax evaded, whichever higher.
- **Part A** = invoice/consignment (GSTINs, PINs, doc no/date, HSN, value, reason).
  **Part B** = transport (vehicle no for road; transport doc no for rail/air/ship).
  **EWB not valid for movement until Part B filled** — except < 50 km intra-state (first/last mile).
- **Validity (from first Part B entry, expires midnight of next day):**
  Regular — **200 km → 1 day**, +1 day per extra 200 km (or part).
  **ODC** — **20 km → 1 day**, +1 day per extra 20 km. Extendable (±8 h of expiry), cap 360 days.
- **Generation:** standalone (full payload) **or** by IRN on the IRP (auto Part A;
  `Distance=0` ⇒ auto PIN-to-PIN). Supplier / recipient / transporter can generate.
- **Cancel within 24 h** (not if verified in transit). Other party can **reject within 72 h**.
- **Transport mode:** 1-Road, 2-Rail, 3-Air, 4-Ship. **Vehicle type:** Regular / ODC.
- **Consolidated EWB** (EWB-02, action `GENCEWB`): bundle multiple EWBs in one conveyance.
- **From 01/08/2026 (GSTN advisory 17.06.2026):** Ship-to GSTIN becomes mandatory when
  Ship-to details are present, plus a **Voluntary Closure of EWB** facility/API. See §11.

### EWB API
Same crypto scheme, **separate host + creds**. One `/ewayapi` endpoint, `action`-driven:
`GENEWAYBILL` · `VEHEWB` (Part B) · `CANEWB` · `REJEWB` · `EXTENDVALIDITY` ·
`UPDATETRANSPORTER` · `GENCEWB` · **`CLOSEEWB`** (voluntary closure — §11). GET reads:
`GetEwayBill`, `GetGSTINDetails`, `GetTransporterDetails`.
Prod base `https://api.ewaybillgst.gov.in/v1.03` (+ `/Auth`, `/ewayapi`).
*(Our `ewaybill/client.py` implements all these, incl. `close_ewb` → `/api/ewaybill/close/`.)*

### Common EWB error codes (retrieve full list at runtime via GET Error List)
| Code | Meaning |
|---|---|
| 100 | Invalid JSON |
| 102 | Invalid password / login |
| 106 | Token expired |
| 108 | Invalid login credentials |
| 109 | Decryption of data failed |
| 111 | GSTIN not registered to this GSP |
| 238 | Invalid auth token |
| 315 | Validity lapsed — cannot cancel |
| 316 | Cannot cancel a verified EWB |
| 325 | Could not retrieve data |
| 342 | Cannot reject — not the other party |
| 343 | Already cancelled |
| 702 | Distance between PINs too high |
| 4011 | Vehicle number required for Road mode |
| 4014 | Invalid vehicle number format |

---

## 8. NIC best practices (enforce in our integration)

1. **Cache the token/SEK** — never auth per transaction (risk of NIC blocking). *(Client already caches.)*
2. **Refresh ~10 min before the 6 h expiry**; recheck & resubmit requests that failed in the window.
3. **Persist** `AckNo, AckDt, Irn, SignedInvoice, SignedQRCode` with the source record
   (IRP purges after 24 h; QR must be printed). *(Not yet done — needs a model.)*
4. **Validate schema + regexes + arithmetic client-side before calling Generate IRN.** *(Not yet done.)*
5. **Handle `Status`/`ErrorDetails`** — correct & resubmit; surface codes, don't just 502.
6. **Don't store/hardcode NIC's SSL cert** — it rotates; rely on CA trust, TLS 1.2+.

## 9. Gaps between the ported code and these practices (future work)
The current `einvoice`/`ewaybill` apps are a faithful thin client + crypto layer. To
become a production integration they still need:
- ✅ **Persistence models** for IRN/EWB responses (practice #3) — `IrnRecord`, `EwayBill`.
- ✅ **Pre-submit validation** (regexes §6 + arithmetic §4) (practice #4) — `einvoice/validation.py`.
- ✅ **Structured error surfacing** mapped to §3/§7 code tables (practice #5) — `einvoice/errors.py`.
- An **invoice builder** that maps OMS order/sales data → the §5 schema paths.
- ✅ **01/08/2026 readiness (§11):** Ship-to GSTIN validations (5002/2323/2325) in
  `validation.validate_invoice`; `validation.validate_ewb_by_irn` enforces `ExpShipDtls.Gstin`
  (5001/4074); `ewaybill.close_ewb` (`CLOSEEWB`) at `/api/ewaybill/close/`. The *caller* must
  still populate `ShipDtls.Gstin` / `ExpShipDtls.Gstin` in payloads (enforced by validation).

---

## 10. e-Way Bill by IRN — field mapping (IRN payload → generated EWB)

When an EWB is generated from an IRN, NIC derives the EWB fields from the registered
invoice. Only **transport** fields (and, from 01/08/2026, `ExpShipDtls.Gstin`) are sent
in the EWB-by-IRN call; everything else is auto-filled from the invoice as below.

- `supplyType` = **`O`** (Outward) — always.
- `subSupplyType` = **`3`** (Export) when `TranDtls.SupTyp` is `EXPWP`/`EXPWOP`; else **`1`** (Supply).
- `transactionType`: **1** Regular (no Dispatch-from, no Ship-to) · **2** Ship-to (only Ship-to) ·
  **3** Dispatch-from (only Dispatch-from) · **4** Combination (both).
- **Doc:** `docType`←`DocDtls.Typ` (only `INV`) · `docNo`←`DocDtls.No` · `docDate`←`DocDtls.Dt`.
- **From (consignor):** `fromGstin`←`SellerDtls.Gstin` · `fromTrdName`←`SellerDtls.TrdNm` ·
  `fromAddr1/2`,`fromPlace`,`fromPincode`←`DispDtls.*` **else** `SellerDtls.*` ·
  `fromStateCode`←`SellerDtls.Stcd` · `actFromStateCode`←`DispDtls.Stcd` (else seller Stcd).
- **To (consignee):** `toGstin`←`BuyerDtls.Gstin` · `toTrdName`←`BuyerDtls.TrdNm` ·
  `toAddr1/2`,`toPlace`,`toPincode`←`ShipDtls.*` **else** `BuyerDtls.*` ·
  `toStateCode`←`BuyerDtls.Stcd` · `actToStateCode`←`ShipDtls.Stcd` (else buyer Stcd).
- **Items (first 250 only):** `productDesc`←`Item.PrdDesc` (first 100 chars) · `hsnCode`←`Item.HsnCd` ·
  `quantity`←`Item.Qty` · `qtyUnit`←`Item.Unit` · `taxableAmount`←`Item.AssAmt` ·
  `cgstRate`=`sgstRate`=`Item.GstRt/2` · `igstRate`=`Item.GstRt` · `cessRate`←`Item.CesRt` ·
  `cessNonadvol`=`0` · `productName` not stored.
- **Totals:** `totalValue`←`ValDtls.AssVal` · `cgstValue/sgstValue/igstValue`←`ValDtls.CgstVal/SgstVal/IgstVal` ·
  `cessValue`←`ValDtls.CesVal` (cess + cessNonAdvol at item level) · `cessNonAdvolValue`=`0` ·
  **`otherValue` = `ValDtls.OthChrg + ValDtls.StCesVal − ValDtls.Discount + ValDtls.RndOffAmt`**
  (StCesVal = item StateCesAmt + StateCesNonAdvlAmt) · `totInvValue`←`ValDtls.TotInvVal`.
- **Transport (sent in the EWB-by-IRN call):** `transMode`←`EwbDtls.TransMode` (1-Road 2-Rail 3-Air 4-Ship) ·
  `transDistance`←`EwbDtls.Distance` · `transporterName`←`EwbDtls.TransName` ·
  `transporterId`←`EwbDtls.TransId` (15-digit) · `transDocNo`←`EwbDtls.TransDocNo` ·
  `transDocDate`←`EwbDtls.TransDocDt` · `vehicleNo`←`EwbDtls.VehNo` · `vehicleType`←`EwbDtls.VehType`.

---

## 11. GSTN Advisory 17.06.2026 — mandatory Ship-to GSTIN + EWB Voluntary Closure
**Production effective 01/08/2026; already in Sandbox.** Applies to: Generate-IRN-with-EWB,
Generate-EWB-by-IRN, Bill-to/Ship-to, and Combination transactions.

### 11.1 Ship-to GSTIN becomes mandatory
- **Generate IRN (with EWB required):** `ShipDtls.Gstin` is **conditionally mandatory** —
  required whenever Ship-to Legal Name + Address are provided. Use **`URP`** if the
  Ship-to party is unregistered / GSTIN not applicable.
- **e-Way Bill by IRN API:** a new **`ExpShipDtls.Gstin`** field is added and is **mandatory**
  (send the Ship-to GSTIN or `URP`). An optional **`ExpShipDtls.TrdNm`** (trade name) is also added.

### 11.2 Ship-to GSTIN validation rules
| Situation | Treatment |
|---|---|
| Valid Ship-to GSTIN, different from Bill-to | Accepted (subject to validation) |
| Invalid Ship-to GSTIN | Rejected |
| Same GSTIN in Bill-to and Ship-to | **Not permitted** in Bill-to/Ship-to transactions |
| Ship-to GSTIN not available | Enter `URP` where applicable |

- **Export EWBs:** Ship details (incl. GSTIN) from IRN **may be replaced** at EWB-by-IRN time; `URP` allowed.
- **B2B / SEZ:** Ship details from IRN **cannot be replaced**; but if GSTIN was absent at IRN time it
  may be supplied at EWB-by-IRN. Old IRNs where Bill-to == Ship-to GSTIN → a *regular* EWB is generated.

### 11.3 New validations / error codes
**Generate IRN + EWB together:** `5002` Ship-to GSTIN mandatory when Ship details present ·
`2323` Bill-to and Ship-to GSTIN must differ · `2325` Ship-to state code must match GSTIN state ·
`3039` Ship-to PIN must belong to Ship-to state.
**e-Way Bill by IRN:** `5001` `ExpShipDtls.Gstin` mandatory · `2324` (B2B/SEZ) Ship details from IRN
cannot be replaced · `4074` Ship-to state code must match GSTIN state · `3039` Ship-to PIN vs state.

### 11.4 Voluntary Closure of e-Way Bill
- New facility to **close an EWB after delivery is complete** (voluntary). May be done by
  supplier, recipient, transporter, or the driver/authorised person (mobile-based, portal only).
  Closure is EWB-wise or date-wise.
- **EWB Closure API** — inputs: **EWB number, closure date, remarks**. (Mobile-number capture and
  closed-EWB retrieval are **not** available via API yet.)
- **Status:** a separate `Closed` status is proposed later; during stabilisation the existing
  Active/Cancelled/Discarded framework continues, and post-closure actions (Update Transporter,
  Extend Validity, Vehicle Updation) **remain allowed** for now.
