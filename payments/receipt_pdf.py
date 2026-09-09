"""OMS-generated, SAP-style payment receipt PDF.

This renders a payment receipt that VISUALLY resembles the Finance team's SAP
Incoming Payment Crystal layout, built ENTIRELY from data OMS already stores on
the ``PaymentReceipt`` (header, method line, invoice allocations, cash
denominations). It is deliberately NOT the SAP-rendered Crystal Report file —
the real Crystal Reporting-Service integration is a separate, future phase
(Option B). See docs/SAP_CRYSTAL_RECEIPT_INTEGRATION.md.

Guarantees:
  * No SAP call is made to render the PDF.
  * The database is never mutated.
  * No temporary files are written — the PDF is built in memory and returned as
    bytes.

The caller (the view) is responsible for the POSTED-only gate and access
control; this module only draws whatever receipt it is handed.
"""

from decimal import Decimal
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from . import receipt_invoices

# Monochrome, document-style palette — no dashboard colour, matching an SAP
# printout rather than an app screen.
_INK = colors.HexColor("#111111")
_MUTED = colors.HexColor("#555555")
_RULE = colors.HexColor("#000000")
_BOX = colors.HexColor("#888888")
_ZEBRA = colors.HexColor("#F2F2F2")

_METHOD_LABELS = {
    "CASH": "Cash",
    "UPI": "UPI",
    "CHEQUE": "Cheque",
}


def _style(name, size, *, bold=False, color=_INK, leading=None, align=0,
           space_after=0):
    return ParagraphStyle(
        name,
        fontName="Helvetica-Bold" if bold else "Helvetica",
        fontSize=size,
        leading=leading or size + 2,
        textColor=color,
        alignment=align,
        spaceAfter=space_after,
    )


def _money(value, currency="INR"):
    """Format a Decimal as a plain, comma-grouped amount with the currency."""
    try:
        amount = Decimal(value or 0)
    except Exception:  # noqa: BLE001 — never let formatting break the PDF
        amount = Decimal(0)
    # Indian-style grouping is overkill here; plain thousands grouping reads
    # cleanly and matches the sample layout.
    return f"{currency} {amount:,.2f}"


def _fmt_date(value):
    return value.strftime("%d-%b-%Y") if value else ""


def _section_heading(text):
    return Paragraph(text.upper(), _style("h", 8.5, bold=True, color=_MUTED))


def _kv_table(rows, col_widths):
    """A borderless label/value table. A value may be a string or an existing
    flowable (e.g. a pre-styled Paragraph), which is passed through as-is."""
    data = [
        [Paragraph(str(k), _style("k", 9, color=_MUTED)),
         v if isinstance(v, Paragraph) else Paragraph(str(v), _style("v", 9, color=_INK))]
        for k, v in rows
    ]
    t = Table(data, colWidths=col_widths, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def _payment_rows(entry):
    """Method-specific detail lines for the single tender on the receipt.

    The model only ever stores CASH / UPI / CHEQUE (PaymentMethodEntry.Method).
    Only fields that carry a value are shown; nothing is invented.
    """
    label = _METHOD_LABELS.get(entry.method, entry.method)
    rows = [("Payment Method", label)]
    if entry.method == "UPI" and entry.upi_reference:
        rows.append(("UPI / UTR Reference", entry.upi_reference))
    if entry.method == "CHEQUE":
        if entry.cheque_number:
            rows.append(("Cheque Number", entry.cheque_number))
        if entry.bank_name:
            rows.append(("Customer Cheque Bank", entry.bank_name))
        if entry.cheque_date:
            rows.append(("Cheque Date", _fmt_date(entry.cheque_date)))
    return rows


def _append_denominations(story, denoms, currency):
    """Append the cash-denomination breakdown table when denoms are present."""
    if not denoms:
        return
    story.append(Spacer(1, 5))
    story.append(Paragraph("Cash Denominations",
                           _style("dh", 8.5, bold=True, color=_MUTED)))
    ddata = [["Note", "Qty", "Line Total"]]
    counted = Decimal(0)
    for d in denoms:
        ddata.append([f"₹{d.denomination}", str(d.quantity),
                      _money(d.line_total, currency)])
        counted += Decimal(d.line_total)
    ddata.append(["", "Total Counted", _money(counted, currency)])
    dt = Table(ddata, colWidths=[30 * mm, 30 * mm, 45 * mm], hAlign="LEFT")
    dt.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, _BOX),
        ("LINEABOVE", (0, -1), (-1, -1), 0.5, _BOX),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(dt)


def _fetch_sap_document(receipt):
    """The LIVE SAP IncomingPayment JSON for this receipt, or None.

    Fetched via the Service Layer by the stored sap_doc_entry so the receipt
    shows the EXACT values SAP holds (address, remarks, branch, references,
    invoices, currency, …) rather than OMS's stored copy.

    Best-effort ONLY: a SAP outage / 404 / any error must NOT break the receipt.
    We fall back to the OMS-stored fields, and the caller degrades gracefully.
    """
    import logging

    doc_entry = getattr(receipt, "sap_doc_entry", None)
    if not doc_entry:
        return None
    company = getattr(receipt, "company", "") or ""
    try:
        from . import sap_client, services
        # Resolve the SAP company DB from the ENVIRONMENT, NOT from
        # receipt.company_db — that column can hold a stale name from when the
        # receipt was created (e.g. an old TEST_OIL_15122025 before the DB was
        # renamed). Settings are the single source of truth.
        company_db = services.resolve_company_db(company)
        if not company_db:
            return None
        return sap_client.fetch_document(int(doc_entry), company_db,
                                         entity="IncomingPayments")
    except Exception as exc:  # noqa: BLE001 — SAP down must not break the receipt
        logging.getLogger("payments").warning(
            "receipt pdf: SAP document fetch failed for DocEntry %s: %s",
            doc_entry, str(exc).splitlines()[0][:200])
        return None


def _sap_str(sap, key):
    v = (sap or {}).get(key)
    return str(v).strip() if v not in (None, "") else ""


def _fetch_party_master(receipt, sap):
    """OCRD party detail (billing address + payment-terms label) for the receipt's
    party, or None. Keyed by the SAP payment's CardCode (falling back to OMS's
    card_code). Best-effort: any SAP/OCRD error returns None so the receipt still
    renders without the address.
    """
    import logging

    card_code = _sap_str(sap, "CardCode") or (getattr(receipt, "card_code", "") or "")
    company = getattr(receipt, "company", "") or ""
    card_code = card_code.strip()
    if not card_code or not company:
        return None
    try:
        from . import hana_queries
        return hana_queries.fetch_party_details(company=company, card_code=card_code)
    except Exception as exc:  # noqa: BLE001 — OCRD lookup must not break the receipt
        logging.getLogger("payments").warning(
            "receipt pdf: OCRD lookup failed for %s/%s: %s",
            company, card_code, str(exc).splitlines()[0][:200])
        return None


def _to_dec(value):
    from decimal import Decimal
    try:
        return Decimal(str(value or 0))
    except Exception:  # noqa: BLE001
        return Decimal(0)


def _sap_addr(sap):
    """The full SAP address block (\\r-separated) normalised to newlines."""
    addr = _sap_str(sap, "Address")
    return addr.replace("\r\n", "\n").replace("\r", "\n").strip()


def _sap_date(sap, key):
    """Format a SAP ISO datetime string (e.g. '2026-06-12T00:00:00Z') as a date."""
    raw = _sap_str(sap, key)
    if not raw:
        return ""
    try:
        return f"{int(raw[8:10]):02d}-{_MONTHS[int(raw[5:7]) - 1]}-{raw[0:4]}"
    except Exception:  # noqa: BLE001
        return raw


def _money_str(value, currency="INR"):
    """Format a numeric string as a grouped amount with the currency."""
    from decimal import Decimal
    try:
        return f"{currency} {Decimal(str(value)):,.2f}"
    except Exception:  # noqa: BLE001
        return f"{currency} {value}"


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _sap_payment_total(sap):
    """The payment's real total from SAP: DocTotal, or the tender sum when
    DocTotal is null (common on ORCT). Returns a Decimal, or None if no SAP.
    """
    from decimal import Decimal

    if not sap:
        return None
    doc_total = _to_dec(sap.get("DocTotal"))
    if doc_total > 0:
        return doc_total
    checks = sum((_to_dec(c.get("CheckSum"))
                  for c in (sap.get("PaymentChecks") or [])), Decimal(0))
    return _to_dec(sap.get("CashSum")) + _to_dec(sap.get("TransferSum")) + checks


def _sap_method_and_amount(sap):
    """(method label, amount Decimal) from the SAP payment sums.

    SAP records the tender in one of CashSum / TransferSum / a cheque line, so
    the receipt names the same tender SAP posted, with SAP's own figure.
    """
    from decimal import Decimal

    def dec(key):
        try:
            return Decimal(str((sap or {}).get(key) or 0))
        except Exception:  # noqa: BLE001
            return Decimal(0)

    cash, transfer = dec("CashSum"), dec("TransferSum")
    checks = (sap or {}).get("PaymentChecks") or []
    if cash > 0:
        return "Cash", cash
    if transfer > 0:
        return "Transfer", transfer
    if checks:
        total = sum((dec_c for dec_c in (
            Decimal(str(c.get("CheckSum") or 0)) for c in checks)), Decimal(0))
        return "Cheque", total
    return "", Decimal(0)


def build_receipt_pdf(receipt) -> bytes:
    """Render ``receipt`` to PDF bytes. No SAP call; no DB write."""
    currency = getattr(receipt, "currency", None) or "INR"
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"Receipt {receipt.receipt_no}",
        author="OMS",
    )
    story = []

    # SAP IS THE SOURCE OF TRUTH. The live SAP payment document (fetched by the
    # stored sap_doc_entry) drives EVERY printed field — customer, address,
    # branch, document number, posting date, currency, amount, payment method,
    # invoices settled and remarks — so the receipt always shows exactly what SAP
    # holds, even if OMS's stored copy has since drifted. OMS-stored values are
    # used ONLY as a fallback when SAP is unreachable (best-effort; the receipt
    # never fails for a SAP outage).
    sap = _fetch_sap_document(receipt)
    currency = _sap_str(sap, "DocCurrency") or currency
    # Party master (OCRD): billing address block + payment-terms label, keyed by
    # the payment's CardCode. Best-effort — omitted if SAP/OCRD is unreachable.
    party = _fetch_party_master(receipt, sap)

    # ---- Title -----------------------------------------------------------
    # SAP titles the layout simply "Receipt"; mirror that.
    title_tbl = Table(
        [[Paragraph("Receipt", _style("title", 16, bold=True)),
          Paragraph("Original", _style("orig", 9, color=_MUTED, align=2))]],
        colWidths=[130 * mm, 44 * mm])
    title_tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(title_tbl)
    story.append(Paragraph(
        f"{receipt.company} &nbsp;·&nbsp; OMS Receipt {receipt.receipt_no}",
        _style("sub", 9, color=_MUTED)))
    story.append(Spacer(1, 4))
    story.append(Table([[""]], colWidths=[174 * mm],
                       style=TableStyle([("LINEBELOW", (0, 0), (-1, -1), 1, _RULE)])))
    story.append(Spacer(1, 8))

    # ---- Bill To  +  Information (two columns) --------------------------
    # Customer NAME comes from SAP's ORCT CardName WHEN the payment is
    # customer-type (DocType 'rCustomer') — there CardName is the real customer.
    # For an account-type receipt (DocType 'rAccount') CardName is the BANK
    # account, not the customer, so we use the OMS-captured customer name there.
    # Customer CODE always comes from SAP's ORCT CardCode. Everything else on the
    # receipt comes from SAP.
    # For a customer-type receipt SAP's CardName/CardCode ARE the customer, so
    # use them. For an account-type (bank) receipt they are the bank G/L account,
    # so both name and code come from OMS's captured customer instead.
    doc_type = _sap_str(sap, "DocType").lower()
    sap_name = _sap_str(sap, "CardName")
    sap_code = _sap_str(sap, "CardCode")
    if sap and doc_type == "rcustomer" and sap_name:
        name = sap_name
        code = sap_code or receipt.card_code or ""
    else:
        name = receipt.card_name or sap_name or receipt.card_code or ""
        code = receipt.card_code or sap_code or ""
    bill_rows = [("Customer", Paragraph(name, _style("n", 9.5, bold=True)))]
    if code:
        bill_rows.append(("Customer Code", code))
    # Billing address: prefer the OCRD party master (full multi-line block, like
    # the SAP receipt), fall back to the payment document's Address field.
    address = ((party or {}).get("address") or "").strip() or _sap_addr(sap)
    if address:
        bill_rows.append((
            "Address",
            Paragraph(address.replace("\n", "<br/>"), _style("addr", 9, leading=12)),
        ))
    branch = _sap_str(sap, "BPLName")
    if branch:
        bill_rows.append(("Branch", branch))

    # All SAP, with OMS fallback only when there is no SAP document at all.
    doc_num = _sap_str(sap, "DocNum") or (receipt.sap_doc_num or "")
    posting = _sap_date(sap, "DocDate") or _fmt_date(receipt.payment_date)
    sap_total = _sap_payment_total(sap) if sap else None
    total_amount = sap_total if sap_total is not None else receipt.total_amount
    info_rows = [
        # SAP labels this "Document Numbering".
        ("Document Numbering", doc_num),
        ("Posting Date", posting),
        ("Currency", currency),
        ("Total Amount", _money(total_amount, currency)),
    ]
    # Payment Terms from the OCRD terms group ("ADVANCE/CASH/0 DAYS"-style), as on
    # the SAP receipt. Shown only when SAP/OCRD supplies it.
    party_terms = (party or {}).get("payment_terms") or ""
    if party_terms:
        info_rows.append(("Payment Terms", party_terms))

    left = [_section_heading("Bill To"), Spacer(1, 3),
            _kv_table(bill_rows, [22 * mm, 60 * mm])]
    right = [_section_heading("Information"), Spacer(1, 3),
             _kv_table(info_rows, [32 * mm, 50 * mm])]
    header = Table([[left, right]], colWidths=[87 * mm, 87 * mm])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(header)
    story.append(Spacer(1, 10))

    # ---- Paid Invoices — SAP's PaymentInvoices[] ------------------------
    # The exact invoice lines SAP settled (positive it_Invoice lines). SAP's
    # line has the applied amount but not the invoice date/total, so those two
    # columns are filled from the OMS allocation snapshot when a match exists.
    allocations = list(receipt.allocations.all())
    story.append(_section_heading("Paid Invoices"))
    story.append(Spacer(1, 3))
    data = None
    # One row per allocation, resolved by the service layer. Previously this
    # matched SAP's PaymentInvoices[] against allocations by sap_doc_num while
    # SAP returns DocEntry there, so the match failed, the Date and Total cells
    # came out blank, and the number printed was SAP's DocEntry for the
    # PAYMENT rather than the invoice's own number.
    invoice_rows = receipt_invoices.invoice_rows_for(
        receipt, allocations=allocations)
    if invoice_rows:
        data = [["Invoice No.", "Invoice Date", "Invoice Total", "Amount Applied"]]
        for row in invoice_rows:
            row_currency = row.get("currency") or currency
            data.append([
                row["invoice_no"],
                # Blank rather than invented when the date is unknown.
                _fmt_date(row["invoice_date"]) if row["invoice_date"] else "",
                # The INVOICE's own total — never the amount applied.
                _money(row["invoice_total"], row_currency)
                if row["invoice_total"] is not None else "",
                _money(row["amount_applied"], row_currency),
            ])
    if data:
        inv = Table(data, colWidths=[44 * mm, 40 * mm, 45 * mm, 45 * mm],
                    hAlign="LEFT")
        style = [
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("TEXTCOLOR", (0, 0), (-1, -1), _INK),
            ("LINEBELOW", (0, 0), (-1, 0), 0.75, _RULE),
            ("LINEBELOW", (0, -1), (-1, -1), 0.5, _BOX),
            ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ]
        for r in range(1, len(data)):
            if r % 2 == 0:
                style.append(("BACKGROUND", (0, r), (-1, r), _ZEBRA))
        inv.setStyle(TableStyle(style))
        story.append(inv)
    else:
        # No invoice settled. An advance (OMS is_advance flag, or SAP settling no
        # invoice) is stated explicitly as "advance — no invoice attached".
        is_advance = bool(getattr(receipt, "is_advance", False))
        note = ("Advance payment — no invoice attached." if is_advance
                else "On-account payment — no invoice attached.")
        story.append(Paragraph(note, _style("adv", 9, color=_MUTED)))
    story.append(Spacer(1, 10))

    # ---- Payment (single tender) — the tender SAP recorded --------------
    # SAP names the tender it posted (Cash / Transfer / Cheque) with SAP's
    # figure. Cash-denomination breakdown is an OMS-only detail shown only when
    # SAP also records a cash tender, so it can never contradict SAP.
    entry = receipt.methods.all().first()
    sap_method, sap_amount = _sap_method_and_amount(sap) if sap else ("", None)
    story.append(_section_heading("Payment"))
    story.append(Spacer(1, 3))
    if sap and sap_method:
        story.append(_kv_table(
            [("Payment Method", sap_method),
             ("Amount", _money(sap_amount, currency))],
            [40 * mm, 90 * mm]))
        cash_entry = entry if (entry and entry.method == "CASH"
                               and sap_method == "Cash") else None
        _append_denominations(
            story,
            list(cash_entry.denominations.all()) if cash_entry else [],
            currency)
    elif sap is None and entry:
        # SAP unreachable — fall back to the OMS tender.
        pay_rows = _payment_rows(entry)
        pay_rows.append(("Amount", _money(entry.amount, currency)))
        story.append(_kv_table(pay_rows, [40 * mm, 90 * mm]))
        denoms = list(entry.denominations.all()) if entry.method == "CASH" else []
        _append_denominations(story, denoms, currency)
    else:
        story.append(Paragraph("No payment method recorded.",
                               _style("nm", 9, color=_MUTED)))
    story.append(Spacer(1, 10))

    # ---- Total (emphasised) — SAP's payment total -----------------------
    total = Table(
        [["TOTAL", _money(total_amount, currency)]],
        colWidths=[130 * mm, 44 * mm])
    total.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 11),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LINEABOVE", (0, 0), (-1, 0), 1, _RULE),
        ("LINEBELOW", (0, 0), (-1, 0), 1, _RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(total)
    story.append(Spacer(1, 10))

    # ---- Remarks — SAP's ORCT.Remarks (source of truth) -----------------
    # Fall back to the OMS-stored remark only when SAP is unreachable.
    remarks = _sap_str(sap, "Remarks") or (receipt.remarks or "").strip()
    if remarks:
        story.append(_section_heading("Remarks"))
        story.append(Spacer(1, 3))
        # Wrap long remarks without truncating.
        story.append(Paragraph(remarks.replace("\n", "<br/>"),
                               _style("rm", 9, leading=13)))
        story.append(Spacer(1, 10))

    # ---- SAP Reference ---------------------------------------------------
    story.append(_section_heading("SAP Reference"))
    story.append(Spacer(1, 3))
    story.append(_kv_table([
        ("OMS Receipt", receipt.receipt_no),
        ("SAP DocEntry", receipt.sap_doc_entry if receipt.sap_doc_entry is not None else ""),
        ("SAP DocNum", receipt.sap_doc_num if receipt.sap_doc_num is not None else ""),
        ("SAP TransId", receipt.sap_trans_id if receipt.sap_trans_id is not None else ""),
    ], [34 * mm, 60 * mm]))
    doc.build(story)
    return buf.getvalue()


def build_receipt_png(receipt, *, dpi: int = 150) -> bytes:
    """Render the same receipt to a PNG image (first page).

    Used so the mobile app can show the receipt inline with a plain <Image>
    (no device PDF viewer needed). Reuses :func:`build_receipt_pdf` — one layout,
    two representations — then rasterises page 1 with PyMuPDF. Pure Python, no
    system dependency. No SAP call; no DB write.
    """
    import fitz  # PyMuPDF — local import so the PDF path never depends on it

    pdf_bytes = build_receipt_pdf(receipt)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        pix = doc[0].get_pixmap(dpi=dpi)
        return pix.tobytes("png")
    finally:
        doc.close()
