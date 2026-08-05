"""Excel export of the invoice register in the office's original sheet layout.

Flattens each invoice's stage-event history back into the one-row-per-invoice
shape of the source workbook (Head Office / Bilty-GRPO / Pre-Audit / Data Entry /
SAP Approval / Save-in-SAP / Payment columns), and appends the newer fields
(current stage, GST number, additional charge).
"""
from datetime import datetime
from io import BytesIO

from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

HEADERS = [
    'Sr no.', 'Invoice date', 'Party name', 'GST Number', 'Invoice no.',
    'Taxable value', 'Gst', 'Rate of GST', 'Additional Charge',
    'Additional Charge Amt', 'Invoice value', 'Category', 'Unit', 'Branch',
    'Mode of Invoice', '',
    'Headoffice (In)', 'Days taken in (parmeet)',
    'Bilty GRPO (in)', 'Tiwari ji Receiving', 'Days taken in GRPO',
    'Pre Audit (In)', 'Audit receiving', 'status', 'Remarks under Audit',
    'Days taken in audit',
    'Data entry', 'Receiving', 'Days taken in entry',
    'Sap approval', 'Sap approval remarks', 'Days taken in sap approval',
    'Jsap approval', 'Jsap approval remarks', 'Days taken in jsap approval',
    'For save in sap', 'Remarks under AP',
    'Discount amt', 'Tds amount', 'Paid Amt', 'Open Invoice', 'Status',
    'Current Stage',
]


def _fmt_date(d):
    if not d:
        return ''
    if isinstance(d, datetime):
        d = timezone.localtime(d)
    return f'{d.day}-{d:%b-%Y}'


def _fmt_amt(v):
    return '' if v is None else f'{v:,.2f}'


def _fmt_days(v):
    return '' if v is None else f'{v:.2f}'


def _recv(note):
    return {'LATE': 'Late received(After 6 PM)', 'ON_TIME': 'Received'}.get(note, '')


def _latest_by_stage(invoice):
    """Most recent visit per stage code (final pass wins for bounced invoices)."""
    latest = {}
    for ev in invoice.events.all():
        code = ev.stage.code
        cur = latest.get(code)
        if cur is None or ev.entered_at > cur.entered_at:
            latest[code] = ev
    return latest


def build_rows(invoices):
    rows = []
    for i, inv in enumerate(invoices, start=1):
        ev = _latest_by_stage(inv)
        entry, bilty = ev.get('entry'), ev.get('bilty_grpo')
        pa, de = ev.get('pre_audit'), ev.get('data_entry')
        sap, sis = ev.get('sap_approval'), ev.get('save_in_sap')
        jsp = ev.get('jsap_approval')
        pay = getattr(inv, 'payment', None)

        rows.append([
            i,
            _fmt_date(inv.invoice_date),
            inv.party_name,
            inv.party_gstin or '',                       # GST Number (after party)
            inv.invoice_number,
            _fmt_amt(inv.taxable_value),
            inv.gst_type.name if inv.gst_type_id else '',
            inv.gst_rate.label if inv.gst_rate_id else '',
            inv.get_additional_charge_type_display() or '',   # after Rate of GST
            _fmt_amt(inv.additional_charge_amount) if inv.additional_charge_type else '',
            _fmt_amt(inv.invoice_value),
            inv.category.name if inv.category_id else '',
            inv.unit.name if inv.unit_id else '',
            inv.branch.name if inv.branch_id else '',
            inv.mode.name if inv.mode_id else '',
            '',  # spacer column
            # Head Office (entry stamp) — parmeet's desk
            _fmt_date(inv.created_at),
            _fmt_days(entry.days_spent) if entry else '',
            # Bilty / GRPO — Transport only (blank otherwise)
            _fmt_date(bilty.entered_at) if bilty else '',
            _recv(bilty.receiving_note) if bilty else '',
            _fmt_days(bilty.days_spent) if bilty else '',
            # Pre-Audit
            _fmt_date(pa.entered_at) if pa else '',
            _recv(pa.receiving_note) if pa else '',
            pa.stage_status if pa else '',
            pa.remarks if pa else '',
            _fmt_days(pa.days_spent) if pa else '',
            # Data Entry
            _fmt_date(de.entered_at) if de else '',
            _recv(de.receiving_note) if de else '',
            _fmt_days(de.days_spent) if de else '',
            # SAP Approval
            _fmt_date(sap.entered_at) if sap else '',
            (sap.stage_status.title() if sap and sap.stage_status else (sap.remarks if sap else '')),
            _fmt_days(sap.days_spent) if sap else '',
            # JSAP Approval — remarks carry JSAP's own reason on a rejection
            _fmt_date(jsp.entered_at) if jsp else '',
            (jsp.remarks or (jsp.stage_status.title() if jsp.stage_status else '')) if jsp else '',
            _fmt_days(jsp.days_spent) if jsp else '',
            # Save in SAP
            _fmt_date(sis.entered_at) if sis else '',
            (sis.remarks or sis.stage_status) if sis else '',
            # Payment
            _fmt_amt(pay.discount_amount) if pay else '',
            _fmt_amt(pay.tds_amount) if pay else '',
            _fmt_amt(pay.paid_amount) if pay else '',
            _fmt_amt(pay.open_balance) if pay else '',
            (pay.get_status_display() if pay else ''),
            # current stage (in-progress) or Completed
            'Completed' if inv.status == 'COMPLETED' else inv.current_stage.name,
        ])
    return rows


def build_workbook(invoices):
    wb = Workbook()
    ws = wb.active
    ws.title = 'Invoices'

    header_fill = PatternFill('solid', fgColor='4F46E5')
    header_font = Font(bold=True, color='FFFFFF')
    ws.append(HEADERS)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)

    for row in build_rows(invoices):
        ws.append(row)

    # Reasonable column widths.
    for idx, header in enumerate(HEADERS, start=1):
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = max(
            12, min(30, len(header) + 2))
    ws.freeze_panes = 'A2'

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
