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
The create posts as the approver user (like SAPInvoiceCreateView) so SAP does
not intercept it into a draft.
"""
import logging

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from tracker.permissions import IsTrackerAP
from .service import SAPServiceLayerManager

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

    def _get(self, branch, path):
        session = SAPServiceLayerManager.get_session(branch)
        resp = session.get(_sl(path), timeout=30)
        if resp.status_code == 401:
            SAPServiceLayerManager.clear_session(branch)
            session = SAPServiceLayerManager.get_session(branch)
            resp = session.get(_sl(path), timeout=30)
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
        resp = self._get(branch, params)
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

    Whether the vendor is subject to withholding, plus the active WT-code master
    that populates the TDS dropdown. SAP enforces the per-vendor allowed list on
    post; we offer the active codes and surface any rejection.
    """

    def get(self, request):
        branch = request.query_params.get("branch") or "OIL"
        card_code = (request.query_params.get("card_code") or "").strip()
        if not card_code:
            return Response({"error": "card_code is required"},
                            status=status.HTTP_400_BAD_REQUEST)

        safe = card_code.replace("'", "''")
        bp = self._get(
            branch,
            f"BusinessPartners('{safe}')"
            "?$select=CardCode,CardName,SubjectToWithholdingTax,WTCode",
        )
        vendor = {}
        if bp.status_code == 200:
            b = bp.json()
            vendor = {
                "card_code": b.get("CardCode"),
                "card_name": b.get("CardName"),
                "subject_to_wt": b.get("SubjectToWithholdingTax") == "boYES",
                "default_wt_code": b.get("WTCode"),
            }

        master = self._get(
            branch,
            "WithholdingTaxCodes"
            "?$select=WTCode,WTName,Rate,OfficialCode,Inactive"
            "&$filter=Inactive eq SAPB1.BoYesNoEnum'tNO'&$top=200",
        )
        codes = []
        if master.status_code == 200:
            codes = [
                {
                    "wt_code": c["WTCode"],
                    "wt_name": c.get("WTName"),
                    "rate": c.get("Rate"),
                    "section": c.get("OfficialCode"),
                }
                for c in master.json().get("value", [])
            ]

        return Response({"branch": branch, "vendor": vendor, "tds_codes": codes})


class APInvoiceCreateView(APIView):
    """POST /api/service-layer/ap/invoice/?branch=OIL

    Body (friendly fields; the SAP payload is built here, never passed through):
      {
        "grpo_entry": 25773,                 # required
        "num_at_card": "INV/2026/01",        # required (vendor's invoice no)
        "doc_date": "2026-08-25",            # optional (yyyy-mm-dd)
        "due_date": "2026-09-14",            # optional
        "comments": "…",                     # optional
        "attachment_entry": 170747,          # optional (e.g. reuse GRPO's)
        "tds": {"liable": true, "wt_code": "TDS"},   # optional
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
        for src, dst in (("doc_date", "DocDate"), ("due_date", "DocDueDate"),
                         ("comments", "Comments")):
            if data.get(src):
                payload[dst] = data[src]
        if data.get("attachment_entry") is not None:
            payload["AttachmentEntry"] = int(data["attachment_entry"])

        tds = data.get("tds") or {}
        if tds.get("liable") and tds.get("wt_code"):
            payload["WTLiable"] = "tYES"
            payload["WithholdingTaxDataCollection"] = [{"WTCode": tds["wt_code"]}]

        # CardCode is normally copied from the GRPO base line, but SAP wants it on
        # the header too; fetch it if the client didn't send it.
        if "CardCode" not in payload:
            look = SAPServiceLayerManager.get_session(branch).get(
                _sl(f"PurchaseDeliveryNotes({int(grpo_entry)})?$select=CardCode"),
                timeout=30,
            )
            if look.status_code == 200:
                payload["CardCode"] = look.json().get("CardCode")

        user = settings.SAP_APPROVER_USER
        password = settings.SAP_APPROVER_PASSWORD
        try:
            session = SAPServiceLayerManager.get_session_for(user, password, branch)
            resp = session.post(_sl("PurchaseInvoices"), json=payload, timeout=40)
            if resp.status_code == 401:
                SAPServiceLayerManager.clear_session(branch)
                session = SAPServiceLayerManager.get_session_for(user, password, branch)
                resp = session.post(_sl("PurchaseInvoices"), json=payload, timeout=40)

            if resp.status_code in (200, 201):
                d = resp.json()
                logger.info("AP invoice posted: DocEntry=%s DocNum=%s (branch=%s, GRPO=%s)",
                            d.get("DocEntry"), d.get("DocNum"), branch, grpo_entry)
                return Response(
                    {"doc_entry": d.get("DocEntry"), "doc_num": d.get("DocNum"),
                     "doc_total": d.get("DocTotal"), "card_code": d.get("CardCode"),
                     "num_at_card": d.get("NumAtCard")},
                    status=status.HTTP_201_CREATED,
                )

            code, msg = _sap_error(resp)
            logger.warning("AP invoice failed (branch=%s GRPO=%s): %s %s",
                           branch, grpo_entry, code, msg)
            return Response({"error": msg, "code": code}, status=resp.status_code)

        except Exception as e:
            logger.exception("AP invoice post errored")
            return Response({"error": str(e)},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
