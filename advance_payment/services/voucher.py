"""The request's voucher in SAP: an OUTGOING PAYMENT, posted when Final approves.

WHAT IS POSTED, FROM WHAT SAP ALREADY DOES
------------------------------------------
Modelled on Oil's own outgoing payments since April 2026 (OVPM, 3,372 live):

    Vendor / Imprest, Against Bill  DocType S, the bills in PaymentInvoices
                                    (957 of 2,154 supplier payments)
    Vendor, Against PO / Imprest    DocType S, ON ACCOUNT: no PaymentInvoices
    Advance / Other                 (794 of them; there are no down payments)
    Employee advance                DocType A, the employee's 1113xxxx advance
                                    G/L in PaymentAccounts (77 of them)

Every one of the 3,372 is a bank TRANSFER; cash goes in CashAccount/CashSum.
A cheque is sent as a transfer from the bank with its number as the
reference, the way the Payments module sends cheque receipts, because this
company's SAP has never had its cheque objects configured.

Series `OP<MM><YY>` for the posting month (NNM1, object 46); the branch is
the paid documents' own (SAP refuses a payment whose branch differs), or the
company's default branch for an advance that pays no document.

POSTING TWICE IS THE FAILURE TO DESIGN AGAINST
----------------------------------------------
A post can time out AFTER SAP committed it. Every voucher therefore carries a
journal memo unique to the request (`OMS AP-2026-0001/1`), and SAP is
searched for that memo before each post: a payment already there is
adopted, not made again. Failed attempts reuse the memo, so a retry after a
lost answer finds the original.

POSTED ONCE, NEVER CHANGED
--------------------------
SAP cannot edit a posted payment's money, and it does not need to: posting
is the last step of the route, after every correction and every approval,
and a completed request is never edited. So there is no cancel or re-post
here. (`SapVoucher.replaced_by` and the SAP_CANCELLED / SAP_REPOSTED log
actions stay in the schema for a reversal done later by hand, if ever.)

Nothing here raises for a SAP refusal: the outcome is written as a
`SapVoucher` row (POSTED or FAILED, with the exact payload and answer) and
returned, so a failure is recorded even though the approval does not advance.
"""
import logging
from dataclasses import dataclass

from django.db.models import Max
from django.utils import timezone

from advance_payment.models import (
    AdvanceRequest,
    DocumentKind,
    Payout,
    PayoutMethod,
    RequestType,
    SapVoucher,
    VoucherObject,
    VoucherStatus,
)
from advance_payment.services import sap as sap_service

logger = logging.getLogger(__name__)

REMARKS_MAX = 254
MEMO_MAX = 50


@dataclass
class Outcome:
    ok: bool
    voucher: SapVoucher = None
    message: str = ''


def _money(value):
    return float(value)


def memo_for(advance):
    """The journal memo of the request's next posting (see the module note)."""
    done = advance.vouchers.filter(
        status__in=[VoucherStatus.POSTED, VoucherStatus.CANCELLED]).count()
    return f'OMS {advance.request_no}/{done + 1}'[:MEMO_MAX]


def live(advance):
    """The POSTED, not-replaced voucher, or None."""
    return (advance.vouchers.filter(status=VoucherStatus.POSTED, replaced_by__isnull=True)
            .order_by('-version').first())


def _branch(advance):
    """`(bpl_id, problem)`: the paid documents' branch, or the default."""
    from payments import sap_company

    docs = list(advance.documents.all())
    if not docs:
        return sap_company.default_bpl_id(advance.company), ''
    kind = docs[0].kind
    found = sap_service.document_branches(advance.company, kind, [d.sap_doc_entry for d in docs])
    missing = [d.sap_doc_num or str(d.sap_doc_entry) for d in docs if d.sap_doc_entry not in found]
    if missing:
        return None, f'{", ".join(missing)} no longer exist in SAP.'
    if kind == DocumentKind.BILL:
        closed = [d.sap_doc_num or str(d.sap_doc_entry) for d in docs if not found[d.sap_doc_entry]['open']]
        if closed:
            return None, f'Bill {", ".join(closed)} is closed or cancelled in SAP and cannot be paid.'
    branches = {found[d.sap_doc_entry]['bpl_id'] for d in docs}
    if len(branches) > 1:
        return None, 'The documents belong to different SAP branches; SAP pays one branch at a time.'
    (bpl_id,) = branches
    return bpl_id if bpl_id is not None else sap_company.default_bpl_id(advance.company), ''


def _remarks(advance):
    docs = list(advance.documents.all())
    parts = [f'OMS {advance.request_no}', advance.partner_name]
    if docs:
        noun = 'Bill' if docs[0].kind == DocumentKind.BILL else 'PO'
        parts.append(f'{noun} ' + ', '.join(d.sap_doc_num or str(d.sap_doc_entry) for d in docs))
    if getattr(advance, 'budget_code', ''):
        parts.append(f'Budget {advance.budget_code}/{advance.sub_budget_code}')
    if advance.remarks:
        parts.append(advance.remarks)
    return ' | '.join(p for p in parts if p)[:REMARKS_MAX]


def build_payload(advance, payout, *, posting_date, series, bpl_id, memo):
    """The Service Layer `VendorPayments` body. Pure: no SAP, no database writes."""
    day = posting_date.isoformat()
    payload = {
        'DocDate': day,
        'TaxDate': day,
        'DocCurrency': advance.currency or 'INR',
        'Remarks': _remarks(advance),
        'JournalRemarks': memo,
    }
    if series is not None:
        payload['Series'] = int(series)
    if bpl_id is not None:
        payload['BPLID'] = int(bpl_id)

    if advance.request_type == RequestType.EMPLOYEE_ADVANCE:
        payload['DocType'] = 'rAccount'
        payload['PaymentAccounts'] = [{
            'AccountCode': advance.partner_code,
            'SumPaid': _money(advance.amount),
            'Decription': advance.partner_name[:50],
        }]
    else:
        payload['DocType'] = 'rSupplier'
        payload['CardCode'] = advance.partner_code
        bills = [d for d in advance.documents.all() if d.kind == DocumentKind.BILL]
        # An on-account payment (a PO advance, an imprest advance) sends NO
        # PaymentInvoices at all; an empty array is itself refused.
        if bills:
            payload['PaymentInvoices'] = [
                {'LineNum': i, 'DocEntry': d.sap_doc_entry,
                 'InvoiceType': 'it_PurchaseInvoice', 'SumApplied': _money(d.amount)}
                for i, d in enumerate(bills)]

    lines = list(payout.lines.all())
    bank = [l for l in lines if l.method != PayoutMethod.CASH]
    cash = [l for l in lines if l.method == PayoutMethod.CASH]
    if bank:
        payload['TransferAccount'] = bank[0].from_account
        payload['TransferSum'] = _money(sum(l.amount for l in bank))
        payload['TransferDate'] = day
        cheques = [l.cheque_number for l in bank if l.method == PayoutMethod.CHEQUE and l.cheque_number]
        if cheques:
            payload['TransferReference'] = ('CHQ ' + ', '.join(cheques))[:50]
    if cash:
        payload['CashAccount'] = cash[0].from_account
        payload['CashSum'] = _money(sum(l.amount for l in cash))
    return payload


def _company_db(advance):
    from payments import sap_company

    return sap_company.resolve_company_db(advance.company)


def _next_version(advance):
    return (advance.vouchers.aggregate(n=Max('version'))['n'] or 0) + 1


def post(advance, *, user):
    """Post the request's outgoing payment. Returns `Outcome`; never raises for SAP."""
    from payments import sap_client

    advance = AdvanceRequest.objects.get(pk=advance.pk)
    payout = Payout.objects.filter(request=advance).first()
    version = _next_version(advance)
    memo = memo_for(advance)
    payload = None

    def failed(message, response=None):
        voucher = SapVoucher.objects.create(
            request=advance, version=version, sap_object=VoucherObject.OUTGOING_PAYMENT,
            status=VoucherStatus.FAILED, payload=payload, response=response,
            error=message, posted_by=user)
        return Outcome(False, voucher, message)

    if payout is None:
        return failed('There are no payment details to post.')
    try:
        posting_date = timezone.localdate()
        bpl_id, problem = _branch(advance)
        if problem:
            return failed(problem)
        series = sap_service.outgoing_payment_series(advance.company, posting_date)
        if series is None:
            return failed(f'SAP has no open outgoing payment series for {posting_date:%B %Y}. '
                          f'Ask Finance to open it.')
        payload = build_payload(advance, payout, posting_date=posting_date, series=series,
                                bpl_id=bpl_id, memo=memo)
        company_db = _company_db(advance)
        # A post whose answer was lost: adopt it rather than pay twice.
        existing = sap_service.outgoing_payment_by_memo(advance.company, memo)
    except sap_service.SapUnavailable as exc:
        return failed(f'Could not reach SAP to prepare the payment: {exc}')
    except Exception as exc:  # noqa: BLE001 — a settings gap must not post blind
        logger.exception('ADVANCE: preparing %s failed', advance.request_no)
        return failed(f'Could not prepare the payment: {exc}')

    if existing:
        body = {'DocEntry': existing['doc_entry'], 'DocNum': existing['doc_num'],
                'adopted': 'found in SAP by its journal memo'}
    else:
        try:
            _, body = sap_client.request('POST', '/VendorPayments', company_db=company_db,
                                         json_body=payload)
        except sap_client.SapError as exc:
            if exc.status_code is None:
                return failed(f'SAP did not answer ({exc}). It may still have posted: approving '
                              f'again checks SAP first and will not post twice.')
            return failed(f'SAP refused the payment: {exc}', response=exc.payload)

    voucher = SapVoucher.objects.create(
        request=advance, version=version, sap_object=VoucherObject.OUTGOING_PAYMENT,
        status=VoucherStatus.POSTED, sap_doc_entry=body.get('DocEntry'),
        sap_doc_num=body.get('DocNum'), payload=payload, response=body, posted_by=user)
    logger.info('ADVANCE: %s posted outgoing payment %s (DocEntry %s)',
                advance.request_no, voucher.sap_doc_num, voucher.sap_doc_entry)
    return Outcome(True, voucher)
