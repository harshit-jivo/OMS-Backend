"""An Expense to a vendor, booked in SAP as an A/P INVOICE (user, 2026-10-20).

The Expense module is the step after the GRPO: what is booked is the bill,
not a payment — the payment is made against it later, as SAP's own bills are.
Expense Audit's approval posts it; nothing else does.

WHAT IS POSTED, FROM WHAT SAP ALREADY DOES
------------------------------------------
Modelled on SAP's own expense bills (OPCH, last 180 days, read 2026-10-20):

    Service bill   DocType S, one line per expense line: its G/L account, the
                   taxable amount, its GST tax code and location, and the five
                   dimensions SAP's posting rules ask of an expense account —
                   Variety, Effective Month, Budget, Sub Budget, State.
    Item bill      Copied from its GRPO (SAP refuses an item A/P invoice
                   without one, rule 1800002): every open GRPO line by
                   BaseType 20 / BaseEntry / BaseLine; SAP copies the item,
                   quantity, price, tax and dimensions from it.

The header: the vendor, the bill number (Vendor Ref. No., UPPER CASE as SAP's
rule 180005 asks), its Doc Date, the posting date (the day it is posted), the
branch and the series of that branch and month (`sap.ap_invoice_series`),
the bill attached (rule 180021), and TDS as SAP's withholding on the bill.

POSTING TWICE IS THE FAILURE TO DESIGN AGAINST
----------------------------------------------
As with the payment (`voucher.py`): the invoice carries a journal memo unique
to the request (`OMS AP-2026-0001/1`), and SAP is searched for it before each
post, so an answer lost after SAP committed adopts the invoice it made.

SAP'S BUDGET RULES
------------------
SAP refuses an expense A/P invoice that did not come from a budget-approved
draft (rules 181111 / 181112 / 181001) unless posted by the users it exempts.
OMS's approvals are that approval: SAP must exempt OMS's posting user
(memo `OMS AP-…`) as it must for the expense payment (461001).
"""
import logging
from dataclasses import dataclass
from decimal import Decimal

from advance_payment.models import AdvanceRequest, SapVoucher, VoucherObject, VoucherStatus
from advance_payment.services import clock
from advance_payment.services import sap as sap_service
from advance_payment.services import voucher as voucher_service

logger = logging.getLogger(__name__)

GRPO_BASE_TYPE = 20
COMMENTS_MAX = 254
DESCRIPTION_MAX = 100


@dataclass
class Outcome:
    ok: bool
    voucher: SapVoucher = None
    message: str = ''


def applies(advance):
    """Whether the request is booked as an A/P invoice: an Expense to a vendor."""
    from advance_payment.models import RequestType
    from advance_payment.services.requests import expense_payee_of

    return advance.request_type == RequestType.EXPENSE and expense_payee_of(advance.partner_code) == 'VENDOR'


def is_item_bill(advance):
    """An Item bill: copied from its GRPO."""
    return advance.expense_category == 'ITEM'


def problems(advance):
    """What the Accounts desk must still set before the invoice can be booked.
    `[]` when ready, or when the request is not booked as an A/P invoice.
    Read from the request alone (no SAP): SAP is asked again when it posts."""
    if not applies(advance):
        return []
    out = []
    if not advance.expense_bpl_id:
        out.append('Choose the Branch the A/P invoice is booked to.')
    if not advance.expense_bill_number.strip():
        out.append("Enter the vendor's Bill Number.")
    if not advance.expense_bill_date:
        out.append("Enter the bill's Doc Date.")
    if is_item_bill(advance):
        if not advance.expense_grpo_entry:
            out.append('Choose the GRPO this Item bill is copied from.')
        return out
    lines = list(advance.expense_lines.all())
    for ln in lines:
        missing = [label for label, value in (('Tax Code', ln.tax_code), ('Variety', ln.variety),
                                              ('State', ln.state)) if not value]
        if missing:
            out.append(f'Line {ln.line_no}: choose its {", ".join(missing)}.')
    codes = sorted({ln.tds_code for ln in lines if ln.tds_code})
    if len(codes) > 1:
        out.append(f'An A/P invoice takes one TDS code; the lines have {", ".join(codes)}.')
    return out


def _remarks(advance):
    parts = [f'OMS {advance.request_no}', advance.partner_name,
             'Budget ' + '/'.join(c for c in (advance.budget_code, advance.sub_budget_code) if c)
             if advance.budget_code else '', advance.remarks]
    return ' | '.join(p for p in parts if p)[:COMMENTS_MAX]


def charges_gst(tax_codes, codes):
    """Whether any of `codes` charges GST on the bill (a rate above 0, not
    reverse charge): its series is the branch's GST one (`_G`), else `_B`."""
    by_code = {t['code']: t for t in tax_codes}
    return any(by_code.get(c) and not by_code[c]['reverse_charge'] and Decimal(by_code[c]['rate']) > 0
               for c in codes)


def build_payload(advance, *, posting_date, series, bpl_id, memo, grpo=None, default_location=None):
    """The Service Layer `PurchaseInvoices` body. Pure: no SAP, no database writes.

    `grpo`: an Item bill's GRPO (`sap.open_grpos`), whose open lines it copies.
    `default_location`: the branch's GST location, for a line without its own.
    """
    day = posting_date.isoformat()
    payload = {
        'CardCode': advance.partner_code,
        'DocDate': day,
        'TaxDate': (advance.expense_bill_date or posting_date).isoformat(),
        'NumAtCard': advance.expense_bill_number.strip().upper()[:100],
        'BPL_IDAssignedToInvoice': int(bpl_id),
        'JournalMemo': memo,
        'Comments': _remarks(advance),
    }
    if advance.expense_due_date:
        payload['DocDueDate'] = advance.expense_due_date.isoformat()
    if series is not None:
        payload['Series'] = int(series)
    lines = list(advance.expense_lines.all())
    if grpo is not None:
        payload['DocType'] = 'dDocument_Items' if grpo.get('category') == 'ITEM' else 'dDocument_Service'
        code = advance.expense_tds_code
        payload['DocumentLines'] = [
            {'BaseType': GRPO_BASE_TYPE, 'BaseEntry': int(grpo['doc_entry']), 'BaseLine': int(ln['line_num']),
             **({'WTLiable': 'tYES'} if code else {})}
            for ln in grpo['lines']]
        codes = [code] if code else []
    else:
        payload['DocType'] = 'dDocument_Service'
        month = advance.effect_month
        payload['DocumentLines'] = [{
            'AccountCode': ln.gl_account,
            'ItemDescription': (ln.remarks or ln.gl_name or advance.partner_name)[:DESCRIPTION_MAX],
            'LineTotal': float(ln.taxable_amount),
            'TaxCode': ln.tax_code,
            **({'LocationCode': int(ln.location or default_location)} if (ln.location or default_location) else {}),
            'CostingCode': ln.variety,
            'CostingCode2': ln.effect_month or month,
            'CostingCode3': advance.budget_code,
            'CostingCode4': advance.sub_budget_code,
            'CostingCode5': ln.state,
            'WTLiable': 'tYES' if ln.tds_code else 'tNO',
        } for ln in lines]
        codes = sorted({ln.tds_code for ln in lines if ln.tds_code})
    if codes:
        payload['WithholdingTaxDataCollection'] = [{'WTCode': c} for c in codes]
    return payload


def post(advance, *, user):
    """Book the request's A/P invoice. Returns `Outcome`; never raises for SAP."""
    from payments import sap_client

    advance = AdvanceRequest.objects.get(pk=advance.pk)
    version = voucher_service._next_version(advance)
    memo = voucher_service.memo_for(advance)
    payload = None
    attachment_entry, attachment_error = None, ''

    def failed(message, response=None):
        row = SapVoucher.objects.create(
            request=advance, version=version, sap_object=VoucherObject.PURCHASE_INVOICE,
            status=VoucherStatus.FAILED, payload=payload, response=response, error=message, posted_by=user)
        return Outcome(False, row, message)

    gaps = problems(advance)
    if gaps:
        return failed('Not sent to SAP: ' + ' '.join(gaps))
    try:
        posting_date = clock.today()
        options = sap_service.ap_invoice_options(advance.company)
        branch = next((b for b in options['branches'] if b['bpl_id'] == advance.expense_bpl_id), None)
        if branch is None:
            return failed(f'Branch {advance.expense_bpl_name or advance.expense_bpl_id} is not an active SAP '
                          f'branch. Send it back to Accounts to choose another.')
        grpo = None
        if is_item_bill(advance):
            found = sap_service.open_grpos(advance.company, advance.partner_code,
                                           doc_entry=advance.expense_grpo_entry)
            if not found:
                return failed(f'GRPO {advance.expense_grpo_num or advance.expense_grpo_entry} is no longer open '
                              f'in SAP (already invoiced, closed or cancelled). Send it back to Accounts.')
            grpo = found[0]
            tax = [ln['tax_code'] for ln in grpo['lines']]
        else:
            tax = [ln.tax_code for ln in advance.expense_lines.all()]
        series = sap_service.ap_invoice_series(
            advance.company, advance.expense_bpl_id, posting_date,
            gst=charges_gst(options['tax_codes'], tax), service=grpo is None or grpo.get('category') != 'ITEM')
        if series is None:
            return failed(f'SAP has no open A/P invoice series for {branch["name"]} in {posting_date:%B %Y}. '
                          f'Ask Finance to open it.')
        payload = build_payload(advance, posting_date=posting_date, series=series[0],
                                bpl_id=advance.expense_bpl_id, memo=memo, grpo=grpo,
                                default_location=branch['location'])
        company_db = voucher_service._company_db(advance)
        existing = sap_service.purchase_invoice_by_memo(advance.company, memo)
    except sap_service.SapUnavailable as exc:
        return failed(f'Could not reach SAP to prepare the A/P invoice: {exc}')
    except Exception as exc:  # noqa: BLE001 — a settings gap must not post blind
        logger.exception('ADVANCE: preparing the A/P invoice of %s failed', advance.request_no)
        return failed(f'Could not prepare the A/P invoice: {exc}')

    if existing:
        body = {'DocEntry': existing['doc_entry'], 'DocNum': existing['doc_num'],
                'DocTotal': existing['doc_total'], 'adopted': 'found in SAP by its journal memo'}
    else:
        # The bill, attached: SAP refuses an A/P invoice without one (rule
        # 180021). An Item bill falls back on its GRPO's (the receiving).
        attachment_entry, attachment_error = voucher_service._attachment(advance, company_db)
        if not attachment_entry and grpo is not None and grpo.get('attachment_entry'):
            attachment_entry = grpo['attachment_entry']
            attachment_error = (attachment_error + ' ' if attachment_error else '') + \
                f"The GRPO's attachment {attachment_entry} was used."
        if not attachment_entry:
            return failed('SAP needs the bill attached to an A/P invoice, and it could not be: '
                          + (attachment_error or 'the request has no file.'))
        payload['AttachmentEntry'] = attachment_entry
        try:
            _, body = sap_client.request('POST', '/PurchaseInvoices', company_db=company_db, json_body=payload)
        except sap_client.SapError as exc:
            if exc.status_code is None:
                return failed(f'SAP did not answer ({exc}). It may still have booked it: approving again '
                              f'checks SAP first and will not book it twice.')
            return failed(f'SAP refused the A/P invoice: {exc}', response=exc.payload)

    row = SapVoucher.objects.create(
        request=advance, version=version, sap_object=VoucherObject.PURCHASE_INVOICE,
        status=VoucherStatus.POSTED, sap_doc_entry=body.get('DocEntry'), sap_doc_num=body.get('DocNum'),
        payload=payload, response=body, posted_by=user, attachment_entry=attachment_entry,
        attachment_error=attachment_error.strip()[:500])
    logger.info('ADVANCE: %s booked A/P invoice %s (DocEntry %s)', advance.request_no, row.sap_doc_num,
                row.sap_doc_entry)
    return Outcome(True, row)
