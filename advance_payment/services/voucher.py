"""The request's voucher in SAP: an OUTGOING PAYMENT, posted when Final approves.

WHAT IS POSTED, FROM WHAT SAP ALREADY DOES
------------------------------------------
Modelled on Oil's own outgoing payments since April 2026 (OVPM, 3,372 live):

    Vendor / Imprest, Against Bill  DocType S, the bills in PaymentInvoices
                                    (957 of 2,154 supplier payments)
    Vendor, Against PO / Imprest    DocType S, ON ACCOUNT: no PaymentInvoices
    Advance / Other                 (794 of them; there are no down payments)
    Expense                         DocType A, one PaymentAccounts line per
                                    expense line, with Variety / Month /
                                    Budget / Sub Budget (dimensions 1-4)
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
from decimal import Decimal

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
from advance_payment.services import payout as payout_service
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
    if kind == DocumentKind.LEDGER:
        # A refund's items are lines of the customer's account: the branch is
        # on the journal, and an item no longer open there cannot be applied.
        items = sap_service.ledger_items(advance.company, advance.partner_code)
        gone = [d.sap_doc_num or str(d.sap_doc_entry) for d in docs
                if (d.sap_doc_entry, d.sap_line) not in items]
        if gone:
            return None, f'{", ".join(gone)} no longer open on the customer’s account in SAP.'
        branches = {items[(d.sap_doc_entry, d.sap_line)]['bpl_id'] for d in docs}
        if len(branches) > 1:
            return None, 'The ledger items belong to different SAP branches; SAP pays one branch at a time.'
        (bpl_id,) = branches
        return bpl_id if bpl_id is not None else sap_company.default_bpl_id(advance.company), ''
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
        noun = {DocumentKind.BILL: 'Bill', DocumentKind.LEDGER: 'Refund against'}.get(docs[0].kind, 'PO')
        parts.append(f'{noun} ' + ', '.join(d.sap_doc_num or str(d.sap_doc_entry) for d in docs))
    if getattr(advance, 'budget_code', ''):
        parts.append('Budget ' + '/'.join(c for c in (advance.budget_code, advance.sub_budget_code) if c))
    if getattr(advance, 'purpose_label', ''):
        parts.append(advance.purpose_label)
    payout = getattr(advance, '_payout_for_remarks', None)
    if advance.request_type == RequestType.EXPENSE:
        tds = sum((ln.tds_amount for ln in advance.expense_lines.all()), Decimal('0'))
        if tds:
            parts.append(f'TDS {tds}')
    elif payout is not None and getattr(payout, 'tds_amount', 0):
        parts.append(f'TDS {payout.tds_code} {payout.tds_rate:g}% {payout.tds_amount}')
    if advance.remarks:
        parts.append(advance.remarks)
    return ' | '.join(p for p in parts if p)[:REMARKS_MAX]


def tds_memo(memo):
    """The TDS journal's memo: the payment's, marked. How a retry finds it again."""
    return f'{memo} TDS'[:MEMO_MAX]


def build_tds_journal(advance, payout, *, posting_date, bpl_id, memo, control_account):
    """The Service Layer `JournalEntries` body booking the TDS: Dr vendor, Cr TDS payable.

    The vendor line comes FIRST: it is line 0, the line the payment applies.
    The same entry JIVO's accountants post by hand when they book TDS.
    """
    day = posting_date.isoformat()
    text = f'TDS {payout.tds_code} {advance.request_no}'[:50]
    branch = {'BPLID': int(bpl_id)} if bpl_id is not None else {}
    return {
        'ReferenceDate': day,
        'DueDate': day,
        'TaxDate': day,
        'Memo': tds_memo(memo),
        'Reference': advance.request_no,
        'JournalEntryLines': [
            {'AccountCode': control_account, 'ShortName': advance.partner_code,
             'Debit': _money(payout.tds_amount), 'Credit': 0.0, 'LineMemo': text, **branch},
            {'AccountCode': payout.tds_account, 'Debit': 0.0,
             'Credit': _money(payout.tds_amount), 'LineMemo': text, **branch},
        ],
    }


def build_expense_tds_journal(advance, *, posting_date, bpl_id, memo):
    """An Expense's TDS journal: Dr each line's G/L (with its four dimensions,
    as SAP's journal rules require on an expense account), Cr each TDS account.

    The payment pays each line's invoice value LESS its TDS, so the G/L ends
    with the invoice value in full and the TDS stands owed to the government.
    """
    from advance_payment.services.sap import EXPENSE_VARIETY

    day = posting_date.isoformat()
    branch = {'BPLID': int(bpl_id)} if bpl_id is not None else {}
    variety = EXPENSE_VARIETY.get(advance.company) or None
    taxed = [ln for ln in advance.expense_lines.all() if ln.tds_amount]
    lines = [{'AccountCode': ln.gl_account, 'Debit': _money(ln.tds_amount), 'Credit': 0.0,
              'LineMemo': f'TDS {ln.tds_code} {advance.request_no} line {ln.line_no}'[:50],
              'CostingCode': variety, 'CostingCode2': ln.effect_month or advance.effect_month,
              'CostingCode3': advance.budget_code, 'CostingCode4': advance.sub_budget_code, **branch}
             for ln in taxed]
    owed = {}
    for ln in taxed:
        owed.setdefault((ln.tds_account, ln.tds_code), Decimal('0'))
        owed[(ln.tds_account, ln.tds_code)] += ln.tds_amount
    lines += [{'AccountCode': account, 'Debit': 0.0, 'Credit': _money(amount),
               'LineMemo': f'TDS {code} {advance.request_no}'[:50], **branch}
              for (account, code), amount in owed.items()]
    return {'ReferenceDate': day, 'DueDate': day, 'TaxDate': day, 'Memo': tds_memo(memo),
            'Reference': advance.request_no, 'JournalEntryLines': lines}


def build_payload(advance, payout, *, posting_date, series, bpl_id, memo, tds_trans_id=None):
    """The Service Layer `VendorPayments` body. Pure: no SAP, no database writes.

    With TDS: the methods pay the net, and against bills the TDS journal's
    vendor line is applied with them, so the bills close in full (bills less
    the journal = what the bank pays). A PO advance is on account: the
    journal stands beside it until accounts set both off against the bill.
    """
    day = posting_date.isoformat()
    advance._payout_for_remarks = payout
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

    if advance.request_type == RequestType.EXPENSE:
        # Straight to the expense accounts, as SAP's own expense payments are
        # (VPM4): one line per expense line, each with Variety (the company's),
        # Month, Budget and Sub Budget — ProfitCenter..ProfitCenter4, SAP's
        # dimensions 1-4. Each pays its invoice value less its TDS (the TDS
        # journal books the rest). Posted by OMS directly (not from a draft):
        # SAP's check 461001 must exempt OMS's payments (B1i, memo `OMS AP-`).
        from advance_payment.services.sap import EXPENSE_VARIETY

        variety = EXPENSE_VARIETY.get(advance.company) or None
        payload['DocType'] = 'rAccount'
        payload['PaymentAccounts'] = [{
            'AccountCode': line.gl_account,
            'SumPaid': _money(line.amount - (line.tds_amount or 0)),
            'Decription': (line.remarks or advance.partner_name)[:50],
            'ProfitCenter': variety,
            'ProfitCenter2': line.effect_month or advance.effect_month,
            'ProfitCenter3': advance.budget_code,
            'ProfitCenter4': advance.sub_budget_code,
        } for line in advance.expense_lines.all()]
    elif advance.request_type == RequestType.EMPLOYEE_ADVANCE:
        payload['DocType'] = 'rAccount'
        payload['PaymentAccounts'] = [{
            'AccountCode': advance.partner_code,
            'SumPaid': _money(advance.amount),
            'Decription': advance.partner_name[:50],
        }]
    elif advance.request_type == RequestType.CUSTOMER:
        # A refund: an outgoing payment TO the customer. Against the ledger,
        # one PaymentInvoices line per item, every SumApplied positive: SAP
        # nets debits (their invoices) against credits (receipts, credit
        # memos) itself, exactly as its own refunds read back. On account:
        # no lines at all.
        from advance_payment.services.requests import LEDGER_OBJECTS

        payload['DocType'] = 'rCustomer'
        payload['CardCode'] = advance.partner_code
        items = [d for d in advance.documents.all() if d.kind == DocumentKind.LEDGER]
        if items:
            payload['PaymentInvoices'] = [
                {'LineNum': i, 'DocEntry': d.sap_doc_entry, 'DocLine': d.sap_line,
                 'InvoiceType': LEDGER_OBJECTS[d.sap_object]['invoice_type'], 'SumApplied': _money(d.amount)}
                for i, d in enumerate(items)]
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
            if tds_trans_id and getattr(payout, 'tds_amount', 0):
                payload['PaymentInvoices'].append({
                    'LineNum': len(bills), 'DocEntry': int(tds_trans_id), 'DocLine': 0,
                    'InvoiceType': 'it_JournalEntry', 'SumApplied': _money(payout.tds_amount)})

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


def _tds_journal(advance, payout, *, posting_date, bpl_id, memo, company_db):
    """Book the TDS journal, or find the one an earlier attempt booked. `(trans_id, problem)`.

    Found again by its memo, and reused only when it still books this TDS: a
    journal left by an attempt before the TDS was changed is not this one.
    """
    from payments import sap_client

    expense = advance.request_type == RequestType.EXPENSE
    tds = payout_service.tds_total(advance, payout)
    try:
        found = _find_tds_journal(advance, memo)
        control = '' if found or expense else sap_service.vendor_control_account(advance.company,
                                                                                 advance.partner_code)
    except sap_service.SapUnavailable as exc:
        return None, f'Could not reach SAP to book the TDS: {exc}'
    if found:
        if found['debit'] != tds:
            return None, (f'SAP already has TDS journal {found["trans_id"]} for this payment, booking '
                          f'{found["debit"]} — not the {tds} now asked. Reverse it in SAP, '
                          f'then approve again.')
        return found['trans_id'], ''
    if expense:
        body = build_expense_tds_journal(advance, posting_date=posting_date, bpl_id=bpl_id, memo=memo)
    elif not control:
        return None, f'SAP has no control account for {advance.partner_code}: the TDS cannot be booked.'
    else:
        body = build_tds_journal(advance, payout, posting_date=posting_date, bpl_id=bpl_id, memo=memo,
                                 control_account=control)
    try:
        _, answer = sap_client.request('POST', '/JournalEntries', company_db=company_db, json_body=body)
    except sap_client.SapError as exc:
        if exc.status_code is None:
            return None, (f'SAP did not answer booking the TDS ({exc}). It may have booked it: approving '
                          f'again finds it by its memo and will not book it twice.')
        return None, f'SAP refused the TDS journal: {exc}'
    trans_id = answer.get('JdtNum') or answer.get('TransId')
    logger.info('ADVANCE: %s booked TDS journal %s (%s)', advance.request_no, trans_id, tds)
    return int(trans_id), ''


def _find_tds_journal(advance, memo):
    """The TDS journal an earlier attempt booked: by memo, and what it debited
    (an Expense's: all its lines; a vendor's: the vendor line)."""
    if advance.request_type == RequestType.EXPENSE:
        return sap_service.journal_total_by_memo(advance.company, tds_memo(memo))
    return sap_service.journal_by_memo(advance.company, tds_memo(memo), advance.partner_code)


def _cancel_tds_journal(trans_id, company_db):
    """Undo the TDS journal of a payment SAP refused. The sentence to add to the message."""
    from payments import sap_client

    try:
        sap_client.request('POST', f'/JournalEntries({int(trans_id)})/Cancel', company_db=company_db)
    except sap_client.SapError as exc:
        logger.error('ADVANCE: could not cancel TDS journal %s: %s', trans_id, exc)
        return (f'Its TDS journal {trans_id} could NOT be cancelled ({exc}): reverse it in SAP '
                f'before approving again.')
    return f'Its TDS journal {trans_id} was cancelled.'


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

    tds_trans_id = None

    def failed(message, response=None):
        voucher = SapVoucher.objects.create(
            request=advance, version=version, sap_object=VoucherObject.OUTGOING_PAYMENT,
            status=VoucherStatus.FAILED, payload=payload, response=response,
            error=message, posted_by=user, tds_trans_id=tds_trans_id)
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
        # SAP as it is NOW, not as it was when raised: an amended, part-paid,
        # closed or cancelled PO / bill must not be paid as if nothing changed.
        # (Not for an adopted payment: our own payment has already moved SAP.)
        from advance_payment.services import reservations

        try:
            changed = [r['message'] for r in reservations.live_check(advance) if not r['ok']]
        except sap_service.SapUnavailable as exc:
            return failed(f'Could not check the documents with SAP before paying: {exc}')
        if changed:
            return failed('SAP has changed since this request was raised: ' + ' '.join(changed)
                          + ' Send it back to Payment to correct it.')
        if payout_service.tds_total(advance, payout):
            tds_trans_id, problem = _tds_journal(advance, payout, posting_date=posting_date,
                                                 bpl_id=bpl_id, memo=memo, company_db=company_db)
            if problem:
                return failed(problem)
            payload = build_payload(advance, payout, posting_date=posting_date, series=series,
                                    bpl_id=bpl_id, memo=memo, tds_trans_id=tds_trans_id)
        try:
            _, body = sap_client.request('POST', '/VendorPayments', company_db=company_db,
                                         json_body=payload)
        except sap_client.SapError as exc:
            if exc.status_code is None:
                return failed(f'SAP did not answer ({exc}). It may still have posted: approving '
                              f'again checks SAP first and will not post twice.')
            message = f'SAP refused the payment: {exc}'
            if tds_trans_id:
                message += ' ' + _cancel_tds_journal(tds_trans_id, company_db)
            return failed(message, response=exc.payload)

    if existing and payout_service.tds_total(advance, payout):
        try:
            found = _find_tds_journal(advance, memo)
        except sap_service.SapUnavailable:
            found = None
        tds_trans_id = found['trans_id'] if found else None
    voucher = SapVoucher.objects.create(
        request=advance, version=version, sap_object=VoucherObject.OUTGOING_PAYMENT,
        status=VoucherStatus.POSTED, sap_doc_entry=body.get('DocEntry'),
        sap_doc_num=body.get('DocNum'), payload=payload, response=body, posted_by=user,
        tds_trans_id=tds_trans_id)
    logger.info('ADVANCE: %s posted outgoing payment %s (DocEntry %s)',
                advance.request_no, voucher.sap_doc_num, voucher.sap_doc_entry)
    return Outcome(True, voucher)
