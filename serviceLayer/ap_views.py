"""AP (accounts payable) invoice data entry — Service Layer endpoints.

Backs the OMS AP invoice entry screen. The flow: the user picks a company
(branch) and an OPEN GRPO (Goods Receipt PO); we copy its lines into an A/P
invoice, letting the user edit only the fields that are genuinely their own —
the vendor's invoice number, dates, TDS and (for short-billing) qty/price.
Everything else (item, warehouse, tax, totals) is copied by SAP from the GRPO.

See docs/ap-invoice-service-layer.md for the verified payload rules:
  * BaseType 20 = GRPO; BaseEntry = the GRPO DocEntry; BaseLine = the line LineNum.
  * BaseLine is NOT a 0..n sequence — copy only lines whose LineStatus is bost_Open.
  * Posting closes the GRPO.
  * TDS via WithholdingTaxDataCollection[{WTCode}] + WTLiable.

Reads use SAPServiceLayerManager.get_session(branch) (cached, per-company).
The create posts a DRAFT (SAP /Drafts, DocObjectCode oPurchaseInvoices) as the
DRAFTER user so it enters SAP's approval procedure; the response carries the
draft's DocEntry/DocNum. The vendor invoice file is uploaded first via
ap/attachment/ (SAP Attachments2) and its AttachmentEntry passed into the draft.
"""
import logging

from django.conf import settings
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from tracker.permissions import IsTrackerAP
from .service import SAPServiceLayerManager

# Max attachment accepted for upload (a vendor invoice PDF/scan).
AP_ATTACHMENT_MAX_BYTES = 15 * 1024 * 1024
# Extensions users actually attach to an A/P invoice; keep it tight.
AP_ATTACHMENT_ALLOWED_EXT = {
    ".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".doc", ".docx",
    ".xls", ".xlsx", ".txt", ".eml", ".msg",
}

logger = logging.getLogger(__name__)

GRPO_BASE_TYPE = 20  # SAP object code for a Goods Receipt PO (PurchaseDeliveryNotes)


def _sl(path):
    return f"{settings.HANA_SERVICE_LAYER_URL}/{path}"


def _sap_error(resp):
    """Turn a Service Layer error body into a friendly (code, message) pair."""
    try:
        err = resp.json().get("error", {})
        msg = err.get("message")
        if isinstance(msg, dict):
            msg = msg.get("value")
        code = str(err.get("code") or "")
    except Exception:
        code, msg = "", resp.text[:300]
    # -5002 is the intermittent attachment-folder/CIFS error (see the doc); make
    # it legible instead of leaking the raw code to the user.
    if code == "-5002":
        msg = ("The attachment could not be stored on the SAP file share "
               "(temporary). The invoice was not posted — please retry.")
    return code, (msg or "SAP error")


class _SLViewMixin:
    """Shared GET-with-one-reauth-retry against the Service Layer."""

    permission_classes = [IsTrackerAP]

    def _get(self, branch, path, page_size=None):
        """GET `path`, retrying once after a re-auth.

        `page_size` sets `Prefer: odata.maxpagesize`. Service Layer caps a
        collection at 20 rows and hands back an `@odata.nextLink` — and `$top`
        does NOT lift that cap, it only bounds it. Any list read here that can
        exceed 20 rows must ask for a bigger page or it is silently truncated,
        which is exactly how the WT-code master used to lose codes (including
        ones actually assigned to the vendor).
        """
        headers = {'Prefer': f'odata.maxpagesize={int(page_size)}'} if page_size else None
        session = SAPServiceLayerManager.get_session(branch)
        resp = session.get(_sl(path), headers=headers, timeout=30)
        if resp.status_code == 401:
            SAPServiceLayerManager.clear_session(branch)
            session = SAPServiceLayerManager.get_session(branch)
            resp = session.get(_sl(path), headers=headers, timeout=30)
        return resp


class OpenGRPOListView(_SLViewMixin, APIView):
    """GET /api/service-layer/ap/open-grpos/?branch=OIL[&vendor=CARD][&search=]

    Lists OPEN GRPOs the user can raise an AP invoice against. Optional vendor
    filter (CardCode) and free-text search on DocNum / vendor invoice no.
    """

    def get(self, request):
        branch = request.query_params.get("branch") or "OIL"
        vendor = (request.query_params.get("vendor") or "").strip()
        search = (request.query_params.get("search") or "").strip()

        filters = ["DocumentStatus eq 'bost_Open'"]
        if vendor:
            safe = vendor.replace("'", "''")
            filters.append(f"CardCode eq '{safe}'")
        if search:
            safe = search.replace("'", "''")
            conds = [f"contains(NumAtCard,'{safe}')"]
            if search.isdigit():
                conds.append(f"DocNum eq {int(search)}")
            filters.append("(" + " or ".join(conds) + ")")

        params = (
            "PurchaseDeliveryNotes"
            "?$select=DocEntry,DocNum,CardCode,CardName,DocDate,DocTotal,"
            "DocCurrency,NumAtCard,AttachmentEntry"
            f"&$filter={' and '.join(filters)}"
            "&$orderby=DocEntry desc&$top=50"
        )
        # page_size, not just $top: SL caps a collection at 20 rows, so this
        # list silently showed 20 of the 50 it asked for.
        resp = self._get(branch, params, page_size=100)
        if resp.status_code != 200:
            code, msg = _sap_error(resp)
            return Response({"error": msg, "code": code}, status=resp.status_code)

        rows = [
            {
                "doc_entry": r["DocEntry"],
                "doc_num": r["DocNum"],
                "card_code": r["CardCode"],
                "card_name": r.get("CardName"),
                "doc_date": r.get("DocDate"),
                "doc_total": r.get("DocTotal"),
                "currency": r.get("DocCurrency"),
                "num_at_card": r.get("NumAtCard"),
                "attachment_entry": r.get("AttachmentEntry"),
            }
            for r in resp.json().get("value", [])
        ]
        return Response({"branch": branch, "grpos": rows})


class GRPODetailView(_SLViewMixin, APIView):
    """GET /api/service-layer/ap/grpo/?branch=OIL&doc_entry=25773  (or &doc_num=)

    Full GRPO for the entry form: header + the OPEN lines only. The `copied`
    fields are shown read-only; the response also carries per-line defaults the
    user may override (quantity, unit_price).
    """

    def get(self, request):
        branch = request.query_params.get("branch") or "OIL"
        doc_entry = request.query_params.get("doc_entry")
        doc_num = request.query_params.get("doc_num")

        if not doc_entry and not doc_num:
            return Response({"error": "doc_entry or doc_num is required"},
                            status=status.HTTP_400_BAD_REQUEST)

        if not doc_entry:
            if not str(doc_num).isdigit():
                return Response({"error": "doc_num must be numeric"},
                                status=status.HTTP_400_BAD_REQUEST)
            lookup = self._get(
                branch,
                "PurchaseDeliveryNotes?$select=DocEntry"
                f"&$filter=DocNum eq {int(doc_num)}&$top=1",
            )
            if lookup.status_code != 200:
                code, msg = _sap_error(lookup)
                return Response({"error": msg, "code": code}, status=lookup.status_code)
            hit = lookup.json().get("value", [])
            if not hit:
                return Response({"error": f"GRPO {doc_num} not found in {branch}."},
                                status=status.HTTP_404_NOT_FOUND)
            doc_entry = hit[0]["DocEntry"]

        resp = self._get(branch, f"PurchaseDeliveryNotes({int(doc_entry)})")
        if resp.status_code != 200:
            code, msg = _sap_error(resp)
            return Response({"error": msg, "code": code}, status=resp.status_code)
        g = resp.json()

        open_lines = [
            {
                "base_line": ln["LineNum"],          # GRPO LineNum -> AP BaseLine
                "item_code": ln.get("ItemCode"),
                "item_description": ln.get("ItemDescription") or ln.get("Dscription"),
                "quantity": ln.get("Quantity"),       # editable default
                "unit_price": ln.get("UnitPrice"),    # editable default
                "warehouse_code": ln.get("WarehouseCode"),
                "tax_code": ln.get("TaxCode"),
                "uom": ln.get("MeasureUnit") or ln.get("UoMCode"),
                "line_total": ln.get("LineTotal"),
            }
            for ln in g.get("DocumentLines", [])
            if ln.get("LineStatus") == "bost_Open"
        ]

        if g.get("DocumentStatus") != "bost_Open" or not open_lines:
            return Response(
                {"error": f"GRPO {g.get('DocNum')} has no open lines to invoice."},
                status=status.HTTP_409_CONFLICT,
            )

        return Response({
            "branch": branch,
            "grpo": {
                "doc_entry": g["DocEntry"],
                "doc_num": g["DocNum"],
                "card_code": g["CardCode"],           # read-only
                "card_name": g.get("CardName"),
                "grpo_num_at_card": g.get("NumAtCard"),
                "doc_date": g.get("DocDate"),
                "doc_total": g.get("DocTotal"),
                "vat_sum": g.get("VatSum"),
                "currency": g.get("DocCurrency"),
                "attachment_entry": g.get("AttachmentEntry"),
                "lines": open_lines,
            },
        })


class VendorTDSView(_SLViewMixin, APIView):
    """GET /api/service-layer/ap/vendor-tds/?branch=OIL&card_code=VENDA001320

    Whether the vendor is subject to withholding, plus the WT codes ACTUALLY
    ASSIGNED to this vendor (BusinessPartners/BPWithholdingTaxCollection),
    enriched with name/rate/section from the WithholdingTaxCodes master.

    SAP enforces the per-vendor allowed list on post, so offering the whole
    master (what this did before) invited a rejection the user could not have
    predicted. If the vendor has no codes assigned we fall back to the full
    active master and say so via `applicable_only=False`, rather than blocking
    the desk with an empty list.
    """

    def get(self, request):
        branch = request.query_params.get("branch") or "OIL"
        card_code = (request.query_params.get("card_code") or "").strip()
        if not card_code:
            return Response({"error": "card_code is required"},
                            status=status.HTTP_400_BAD_REQUEST)

        safe = card_code.replace("'", "''")
        # BPWithholdingTaxCollection is the vendor's assigned WT codes; it comes
        # back with the entity (no $expand needed) when named in $select.
        bp = self._get(
            branch,
            f"BusinessPartners('{safe}')"
            "?$select=CardCode,CardName,SubjectToWithholdingTax,WTCode,"
            "BPWithholdingTaxCollection",
        )
        vendor = {}
        assigned = []
        if bp.status_code == 200:
            b = bp.json()
            vendor = {
                "card_code": b.get("CardCode"),
                "card_name": b.get("CardName"),
                "subject_to_wt": b.get("SubjectToWithholdingTax") == "boYES",
                "default_wt_code": b.get("WTCode"),
            }
            assigned = [w.get("WTCode")
                        for w in (b.get("BPWithholdingTaxCollection") or [])
                        if w.get("WTCode")]

        # Master supplies the display name / rate / section for each code.
        master = self._get(
            branch,
            "WithholdingTaxCodes"
            "?$select=WTCode,WTName,Rate,OfficialCode,Inactive"
            "&$filter=Inactive eq SAPB1.BoYesNoEnum'tNO'&$top=200",
            page_size=500,   # without this SL returns only the first 20 codes
        )
        by_code = {}
        if master.status_code == 200:
            for c in master.json().get("value", []):
                by_code[c["WTCode"]] = {
                    "wt_code": c["WTCode"],
                    "wt_name": c.get("WTName"),
                    "rate": c.get("Rate"),
                    "section": c.get("OfficialCode"),
                }

        if assigned:
            # Keep a code the master doesn't resolve rather than dropping it
            # silently — the user still needs to see it is assigned.
            codes = [by_code.get(w, {"wt_code": w, "wt_name": None,
                                     "rate": None, "section": None})
                     for w in assigned]
            applicable_only = True
        else:
            codes = list(by_code.values())
            applicable_only = False

        return Response({"branch": branch, "vendor": vendor,
                         "tds_codes": codes, "applicable_only": applicable_only})


class APAttachmentUploadView(APIView):
    """POST /api/service-layer/ap/attachment/?branch=OIL   (multipart: file=<file>)

    Uploads ONE file to SAP as a new Attachments2 row in the branch's company DB
    and returns its AbsoluteEntry, which the caller passes back as
    `attachment_entry` when creating the AP invoice draft. This is how the user
    attaches the vendor's invoice at the AP-entry step (SAP re-uploads it here
    rather than reusing the GRPO's copy).

    Creating an Attachments2 row is the ONLY way to obtain an AttachmentEntry —
    the separate file-upload utility puts bytes on the share but yields no entry.
    The file lands in the company's AttachmentsFolderPath. The old -5002
    ("attachments folder ... changed or removed") was a flapping CIFS mount,
    fixed 2026-08-25; one retry is kept in case the mount is mid-reconnect.
    """

    permission_classes = [IsTrackerAP]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        branch = request.query_params.get("branch") or "OIL"
        f = request.FILES.get("file")
        if not f:
            return Response({"error": "file is required (multipart field 'file')"},
                            status=status.HTTP_400_BAD_REQUEST)

        name = f.name or "attachment"
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        if ext not in AP_ATTACHMENT_ALLOWED_EXT:
            return Response({"error": f"File type {ext or '(none)'} is not allowed."},
                            status=status.HTTP_400_BAD_REQUEST)
        if f.size and f.size > AP_ATTACHMENT_MAX_BYTES:
            mb = AP_ATTACHMENT_MAX_BYTES / (1024 * 1024)
            return Response({"error": f"File exceeds the {mb:.0f} MB limit."},
                            status=status.HTTP_400_BAD_REQUEST)

        content = f.read()
        content_type = getattr(f, "content_type", None) or "application/octet-stream"

        def _upload():
            # No json= here: requests sets the multipart Content-Type itself, and
            # the SL session carries no default Content-Type that would override it.
            session = SAPServiceLayerManager.get_session(branch)
            return session.post(
                _sl("Attachments2"),
                files={"files": (name, content, content_type)},
                timeout=60,
            )

        try:
            resp = _upload()
            if resp.status_code == 401:
                SAPServiceLayerManager.clear_session(branch)
                resp = _upload()
            if resp.status_code not in (200, 201):
                code, _ = _sap_error(resp)
                if str(code) == "-5002":     # transient CIFS reconnect — one retry
                    resp = _upload()

            if resp.status_code in (200, 201):
                entry = resp.json().get("AbsoluteEntry")
                logger.info("AP attachment uploaded: AttachmentEntry=%s file=%s (branch=%s)",
                            entry, name, branch)
                return Response({"attachment_entry": entry, "file_name": name},
                                status=status.HTTP_201_CREATED)

            code, msg = _sap_error(resp)
            logger.warning("AP attachment upload failed (branch=%s file=%s): %s %s",
                           branch, name, code, msg)
            return Response({"error": msg, "code": code}, status=resp.status_code)
        except Exception as exc:  # noqa: BLE001 - surface the SL/network failure
            logger.exception("AP attachment upload error (branch=%s)", branch)
            return Response({"error": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


class APInvoiceCreateView(APIView):
    """POST /api/service-layer/ap/invoice/?branch=OIL

    Body (friendly fields; the SAP payload is built here, never passed through):
      {
        "grpo_entry": 25773,                 # required
        "num_at_card": "INV/2026/01",        # required (Vendor Ref. No. / NumAtCard)
        "doc_date": "2026-08-25",            # optional Posting Date  (DocDate)
        "tax_date": "2026-08-24",            # optional Document Date (TaxDate)
        "due_date": "2026-09-14",            # optional Due Date      (DocDueDate)
        "comments": "…",                     # optional
        "attachment_entry": 170747,          # optional (uploaded, or reuse GRPO's)
        "tds": {"liable": true, "wt_codes": ["1041", "1042"]},  # optional, multiple
                                              #   (single "wt_code" still accepted)
        "lines": [                            # required: which GRPO lines + overrides
          {"base_line": 1, "quantity": 696, "unit_price": 23.0},
          {"base_line": 2}
        ]
      }
    quantity/unit_price are OPTIONAL per line — omit to keep the GRPO value.
    """

    permission_classes = [IsTrackerAP]

    def post(self, request):
        branch = request.query_params.get("branch") or "OIL"
        data = request.data or {}

        grpo_entry = data.get("grpo_entry")
        num_at_card = (data.get("num_at_card") or "").strip()
        lines_in = data.get("lines") or []

        if not grpo_entry:
            return Response({"error": "grpo_entry is required"},
                            status=status.HTTP_400_BAD_REQUEST)
        if not num_at_card:
            return Response({"error": "num_at_card (vendor invoice number) is required"},
                            status=status.HTTP_400_BAD_REQUEST)
        if not lines_in:
            return Response({"error": "at least one line is required"},
                            status=status.HTTP_400_BAD_REQUEST)

        document_lines = []
        for ln in lines_in:
            if ln.get("base_line") is None:
                return Response({"error": "each line needs a base_line"},
                                status=status.HTTP_400_BAD_REQUEST)
            row = {
                "BaseType": GRPO_BASE_TYPE,
                "BaseEntry": int(grpo_entry),
                "BaseLine": int(ln["base_line"]),
            }
            # Only send qty/price when the user actually overrode them, so SAP
            # otherwise copies the GRPO values verbatim.
            if ln.get("quantity") not in (None, ""):
                row["Quantity"] = float(ln["quantity"])
            if ln.get("unit_price") not in (None, ""):
                row["UnitPrice"] = float(ln["unit_price"])
            document_lines.append(row)

        card_code = (data.get("card_code") or "").strip()
        payload = {"NumAtCard": num_at_card, "DocumentLines": document_lines}
        if card_code:
            payload["CardCode"] = card_code
        # SAP dates: doc_date -> DocDate (Posting Date), tax_date -> TaxDate
        # (Document Date, the date on the vendor's invoice), due_date ->
        # DocDueDate (Payment Due Date).
        for src, dst in (("doc_date", "DocDate"), ("tax_date", "TaxDate"),
                         ("due_date", "DocDueDate"), ("comments", "Comments")):
            if data.get(src):
                payload[dst] = data[src]
        if data.get("attachment_entry") is not None:
            payload["AttachmentEntry"] = int(data["attachment_entry"])

        # TDS / withholding: accept multiple codes (tds.wt_codes), with
        # back-compat for a single tds.wt_code. Each becomes one row of
        # WithholdingTaxDataCollection; SAP still enforces the vendor's list.
        tds = data.get("tds") or {}
        wt_codes = tds.get("wt_codes")
        if not wt_codes and tds.get("wt_code"):
            wt_codes = [tds["wt_code"]]
        wt_codes = [c for c in (wt_codes or []) if c]
        if tds.get("liable") and wt_codes:
            payload["WTLiable"] = "tYES"
            payload["WithholdingTaxDataCollection"] = [{"WTCode": c} for c in wt_codes]

        # CardCode is normally copied from the GRPO base line, but SAP wants it on
        # the header too; fetch it if the client didn't send it.
        if "CardCode" not in payload:
            look = SAPServiceLayerManager.get_session(branch).get(
                _sl(f"PurchaseDeliveryNotes({int(grpo_entry)})?$select=CardCode"),
                timeout=30,
            )
            if look.status_code == 200:
                payload["CardCode"] = look.json().get("CardCode")

        # AP invoices are entered as DRAFTS so they run through SAP's approval
        # procedure. Post to /Drafts as the DRAFTER user (HANA_USERNAME) — the
        # approver user would bypass approval and post a live invoice, which is
        # exactly what we don't want here. DocObjectCode marks the draft as an
        # A/P invoice; SAP returns the draft's own DocEntry/DocNum (the "draft
        # number") the user quotes to the approver.
        payload["DocObjectCode"] = "oPurchaseInvoices"
        user = settings.HANA_USERNAME
        password = settings.HANA_PASSWORD
        try:
            session = SAPServiceLayerManager.get_session_for(user, password, branch)
            resp = session.post(_sl("Drafts"), json=payload, timeout=40)
            if resp.status_code == 401:
                SAPServiceLayerManager.clear_session(branch)
                session = SAPServiceLayerManager.get_session_for(user, password, branch)
                resp = session.post(_sl("Drafts"), json=payload, timeout=40)

            if resp.status_code in (200, 201):
                d = resp.json()
                logger.info("AP invoice DRAFT created: DocEntry=%s DocNum=%s (branch=%s, GRPO=%s)",
                            d.get("DocEntry"), d.get("DocNum"), branch, grpo_entry)
                return Response(
                    {"is_draft": True,
                     "doc_entry": d.get("DocEntry"), "doc_num": d.get("DocNum"),
                     "doc_total": d.get("DocTotal"), "card_code": d.get("CardCode"),
                     "num_at_card": d.get("NumAtCard")},
                    status=status.HTTP_201_CREATED,
                )

            code, msg = _sap_error(resp)
            logger.warning("AP invoice draft failed (branch=%s GRPO=%s): %s %s",
                           branch, grpo_entry, code, msg)
            return Response({"error": msg, "code": code}, status=resp.status_code)

        except Exception as e:
            logger.exception("AP invoice post errored")
            return Response({"error": str(e)},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
