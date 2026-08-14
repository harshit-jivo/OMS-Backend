"""Payment / deposit business logic. Views stay thin; every multi-table write
is atomic. `orders/views.py` has zero transaction.atomic across 4,537 lines —
this module must not repeat that."""
import logging
from datetime import timedelta
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from approvals import services as approval_services

from . import bank_master, hana_queries
from .models import (
    BankDeposit,
    PaymentMethodEntry,
    PaymentReceipt,
    PaymentStatusHistory,
    SapCompanyMap,
)
from .sap_payloads import build_deposit, build_incoming_payment

logger = logging.getLogger(__name__)


def log_status(document, *, to_status, from_status='', user=None,
               actor_kind='USER', reason='', ip=None,
               action=None, level=None, level_label='',
               sap_doc_entry=None, sap_doc_num=None):
    """Append one row to the activity timeline. Never updated after insert.

    THE single history call for the payments module — approvals, edits and SAP
    attempts all land here, so one query renders the whole timeline.

    `action` says WHAT happened; the status pair says what it changed. When no
    action is given it falls back to STATUS_CHANGED, which keeps older callers
    working without claiming an event they did not record.
    """
    return PaymentStatusHistory.objects.create(
        content_type=ContentType.objects.get_for_model(document.__class__),
        object_id=document.pk,
        action=action or PaymentStatusHistory.Action.STATUS_CHANGED,
        from_status=from_status or '',
        to_status=to_status,
        reason=reason or '',
        actor_kind=actor_kind,
        level=level,
        level_label=level_label or '',
        sap_doc_entry=sap_doc_entry,
        sap_doc_num=sap_doc_num,
        changed_by=user,
        changed_by_username=getattr(user, 'username', '') or '',
        ip_address=ip,
    )


# The old SapPostingHistory.Action values, mapped onto the single timeline.
# Kept as an explicit map so the call sites in sap_poster.py — which are SAP
# posting logic and are deliberately left untouched — keep passing the strings
# they always have.
_SAP_ACTIONS = {
    'POST_STARTED': PaymentStatusHistory.Action.SAP_POST_STARTED,
    'POST_SUCCESS': PaymentStatusHistory.Action.SAP_POSTED,
    'POST_FAILED': PaymentStatusHistory.Action.SAP_FAILED,
    'POST_TIMEOUT': PaymentStatusHistory.Action.SAP_UNKNOWN,
    'MANUAL_RECOVERY': PaymentStatusHistory.Action.SAP_POSTED,
    'RESUBMITTED': PaymentStatusHistory.Action.RESUBMITTED,
}


def record_sap_history(document, *, action, status, response='',
                       doc_entry=None, doc_num=None, user=None,
                       attempt_number=None):
    """Append one SAP posting event to the activity timeline.

    Formerly wrote its own `payment_sap_posting_history` row. That table is
    gone: `payment_status_history` is now the single audit trail, and it carries
    the SAP DocEntry/DocNum columns this needs. The signature is unchanged so
    every call site in `sap_poster.py` — SAP posting logic, deliberately not
    modified — keeps working as-is.

    Two things improve as a side effect. Deposits are recorded too (the old
    table had a FK to PaymentReceipt, so deposit posts were silently dropped),
    and a SAP attempt now interleaves with the approval decisions that led to
    it in one ordered list instead of sitting in a separate table.

    `attempt_number` is accepted and ignored: attempts were only ever a way to
    group rows in a table of nothing but SAP events. In a timeline the ordering
    is the grouping — a SAP_POST_STARTED opens the attempt that the next
    terminal SAP row closes.
    """
    return log_status(
        document,
        to_status=getattr(document, 'status', '') or '',
        from_status='',
        user=user,
        actor_kind='SAP',
        reason=(response or '')[:4000],
        action=_SAP_ACTIONS.get(action,
                                PaymentStatusHistory.Action.STATUS_CHANGED),
        sap_doc_entry=doc_entry,
        sap_doc_num=doc_num,
    )


def resolve_company_db(company):
    """category -> SAP company DB, from the mapping table.

    Replaces resolve_company_db_for_order (sync_service.py:301), which reads
    ITEM categories (a payment has none) and silently routes MART to OIL.
    """
    return _company_mapping(company).company_db


def _company_mapping(company):
    mapping = SapCompanyMap.objects.filter(company=company, is_active=True).first()
    if not mapping:
        raise ValidationError(
            f'No active SAP company mapping for "{company}". '
            f'Configure it before taking payments for this company.')
    return mapping


def resolve_bpl_id(company, receipt=None):
    """The SAP branch a payment must post to.

    SAP refuses a payment whose branch differs from the invoice being paid —
    "Ensure selected branch is the same as the branch of documents to be paid"
    — so the branch is a property of the INVOICE, never a fixed setting. This
    company spreads open invoices across DELHI, FACTORY and PUNJAB, so a single
    default could only ever serve one of them.

    Resolution order:
      1. the branch on the SAP invoice being settled (the authority)
      2. `branches` (sap_sync.Branch) when the invoice names a branch but not
         its id — the mapping table already mirrored from SAP per company
      3. SapCompanyMap.default_bpl_id, ONLY for an advance, which settles no
         invoice and so has no branch to inherit

    Raises ValidationError rather than guessing when invoices disagree, when a
    branch cannot be mapped, or when SAP cannot be reached: posting to the
    wrong ledger is worse than not posting.
    """
    if receipt is None:
        return _company_mapping(company).default_bpl_id

    doc_entries = [a.sap_doc_entry for a in receipt.allocations.all()
                   if a.sap_doc_entry]
    if not doc_entries:
        # An advance settles no invoice, so there is nothing to inherit from:
        # the user says which branch it belongs to.
        chosen = getattr(receipt, 'sap_branch_id', None)
        if chosen:
            logger.info('BPL resolve %s: advance | branch %s | BPLID %s | '
                        'source: user selection', receipt.receipt_no,
                        receipt.sap_branch_name or '(unnamed)', chosen)
            return chosen
        # Legacy rows only — raised before this field existed. A new advance
        # cannot be submitted without a branch (see validate_receipt).
        bpl = _company_mapping(company).default_bpl_id
        logger.info('BPL resolve %s: advance, no branch selected -> BPLID %s '
                    '(source: company default, legacy)',
                    receipt.receipt_no, bpl)
        return bpl

    try:
        rows = hana_queries.fetch_invoice_branches(
            company=company, doc_entries=doc_entries)
    except Exception as exc:                              # noqa: BLE001
        raise ValidationError(
            'Could not read the invoice branch from SAP, so this payment '
            'cannot be posted to the correct branch. Try again shortly.'
        ) from exc

    found = {r['doc_entry'] for r in rows}
    missing = [d for d in doc_entries if d not in found]
    if missing:
        raise ValidationError(
            'Invoice %s no longer exists in SAP.'
            % ', '.join(str(d) for d in missing))

    closed = [r for r in rows
              if r['doc_status'] != 'O' or r['cancelled'] == 'Y']
    if closed:
        raise ValidationError(
            'Invoice %s is closed or cancelled in SAP and cannot be paid.'
            % ', '.join(str(r['doc_num'] or r['doc_entry']) for r in closed))

    branches = {}
    for row in rows:
        bpl = row['bpl_id']
        if bpl is None:
            # Only a name — resolve it through the mapping table.
            bpl = _branch_id_from_name(company, row['bpl_name'])
            row['_source'] = 'branches (by name)'
        else:
            row['_source'] = 'SAP invoice'
        branches.setdefault(bpl, []).append(row)

    if len(branches) > 1:
        names = ', '.join(sorted(
            {f"{r['bpl_name'] or r['bpl_id']}" for r in rows}))
        raise ValidationError(
            f'Selected invoices belong to multiple SAP branches ({names}). '
            f'Please create separate receipts.')

    bpl_id, matched = next(iter(branches.items()))
    sample = matched[0]
    logger.info(
        'BPL resolve %s: invoices %s | branch %s | BPLID %s | source: %s',
        receipt.receipt_no,
        ', '.join(str(r['doc_num'] or r['doc_entry']) for r in matched),
        sample['bpl_name'] or '(unnamed)', bpl_id, sample['_source'])
    return bpl_id


def _branch_id_from_name(company, name):
    """Branch name -> BPLId via the `branches` table mirrored from SAP."""
    from sap_sync.models import Branch

    cleaned = (name or '').strip()
    if not cleaned:
        raise ValidationError(
            'The invoice does not name a SAP branch, so the payment branch '
            'cannot be determined.')
    row = (Branch.objects
           .filter(category=company, bpl_name__iexact=cleaned, is_active=True)
           .first())
    if row is None:
        raise ValidationError(
            f"No SAP Branch mapping exists for branch '{cleaned}'.")
    return row.bpl_id


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------

def validate_receipt(receipt):
    """Cross-field money rules, enforced before submit.

    The DB CheckConstraints catch the crude cases; these are the ones that need
    to look across child rows.
    """
    methods = list(receipt.methods.prefetch_related('denominations'))
    if not methods:
        raise ValidationError('Add at least one payment method.')

    total = sum((entry.amount for entry in methods), Decimal('0'))
    if total != receipt.total_amount:
        raise ValidationError(
            f'Payment methods total {total} but the receipt total is '
            f'{receipt.total_amount}.')

    for entry in methods:
        if entry.method != 'CASH':
            continue
        rows = list(entry.denominations.all())
        if not rows:
            continue                      # breakdown is optional; if given it must balance
        counted = sum((Decimal(r.denomination) * r.quantity for r in rows), Decimal('0'))
        if counted != entry.amount:
            raise ValidationError(
                f'Cash denominations total {counted} but the cash amount is '
                f'{entry.amount}.')

    allocated = sum((a.amount_applied for a in receipt.allocations.all()), Decimal('0'))
    if allocated > receipt.total_amount:
        raise ValidationError(
            f'Allocated {allocated} exceeds the receipt total {receipt.total_amount}.')
    if not receipt.is_advance and not allocated:
        raise ValidationError(
            'Select at least one invoice, or mark the receipt as an advance.')

    # An advance inherits no branch from an invoice, so one must be chosen.
    # Enforced at submit rather than only in the form: the API is reachable
    # without it, and a missing branch is only discovered by SAP otherwise.
    if receipt.is_advance and not allocated and not receipt.sap_branch_id:
        raise ValidationError(
            'Select the SAP branch this advance belongs to.')

    _validate_gl_accounts(receipt.company, {m.method for m in methods})
    return True


def _validate_gl_accounts(company, methods_used):
    """Every method on the document must resolve to a SAP account.

    Without this the payload builder simply OMITS the account field and SAP
    rejects the document — after it has been fully approved, which is the worst
    possible moment to discover a configuration gap. Failing at submit puts the
    error in front of someone who can act on it.

    Also catches a mapping whose account has since been removed from SAP, so a
    payment can never post to a ledger that no longer exists.
    """
    accounts = _bank_accounts_for(company)
    missing = sorted({m for m in methods_used if not accounts.get(m)})
    if not missing:
        return

    labels = dict(PaymentMethodEntry.Method.choices)
    names = ', '.join(labels.get(m, m) for m in missing)
    if missing == ['CASH']:
        where = ('Set the cash G/L account for this company under '
                 'Payments > Configuration > Company mapping.')
    else:
        where = ('Map each of them to a SAP bank account under '
                 'Payments > Configuration > Payment Method Mapping.')
    raise ValidationError(
        f'No SAP account is configured for {names} in {company}. {where}')


@transaction.atomic
def submit_receipt(receipt, user, ctx=None):
    """Send a receipt into the approval chain.

    PENDING_ERROR is submittable: SAP rejected the document, the creator
    corrected it, and it now goes round again on the SAME record. A new approval
    round opens; every earlier round stays in the history.
    """
    if receipt.sap_doc_entry:
        raise ValidationError(
            f'{receipt.receipt_no} is already posted to SAP as DocEntry '
            f'{receipt.sap_doc_entry}. It cannot be submitted again.')

    # A SAP failure sends the approval back to its final rung rather than
    # closing it (see approvals.services.reopen_final_level), so the chain is
    # still open and the approver retries from their own queue. Without this the
    # creator would hit the raw "already has an open approval request" from the
    # engine, which does not explain who now holds the document.
    open_approval = receipt.approvals.filter(
        status__in=['DRAFT', 'PENDING']).first()
    if open_approval is not None:
        raise ValidationError(
            f'{receipt.receipt_no} is still with its approver '
            f'({open_approval.level_label}). They can retry the SAP posting '
            f'from their pending list once the problem is fixed — it does not '
            f'need resubmitting.')
    if receipt.status not in (PaymentReceipt.Status.DRAFT,
                              PaymentReceipt.Status.REJECTED,
                              PaymentReceipt.Status.PENDING_ERROR):
        raise ValidationError(
            f'A {receipt.get_status_display().lower()} receipt cannot be submitted.')

    validate_receipt(receipt)
    previous = receipt.status

    approval_services.submit(
        document=receipt,
        user=user,
        company=receipt.company,
        amount=receipt.total_amount,
        document_number=receipt.receipt_no,
        document_type='PAYMENT',
        ctx=ctx,
    )
    receipt.status = PaymentReceipt.Status.PENDING_APPROVAL
    receipt.save(update_fields=['status', 'updated_at'])
    log_status(receipt, from_status=previous, to_status=receipt.status,
               user=user, reason='Submitted for approval.',
               ip=(ctx or {}).get('ip'))

    # Only a resubmission after a SAP failure belongs in the SAP history — a
    # first submission has not involved SAP at all, and logging it there would
    # imply an attempt that never happened.
    if previous == PaymentReceipt.Status.PENDING_ERROR:
        record_sap_history(
            receipt,
            action='RESUBMITTED',
            status='POSTING',
            response='Payment resubmitted after correction.',
            user=user,
        )
    # The "approval required" notification to the current level's approvers is
    # fired by the approval engine's `submitted` hook (payments.hooks), inside
    # the same transaction opened by approval_services.submit above — so the
    # Notification commits with the submission and rolls back with it. No SAP is
    # involved in this path.
    return receipt


def _bank_accounts_for(company, receipt=None):
    """method -> SAP G/L account, for payload building.

    Every G/L comes from SAP's own House Bank Accounts, chosen by the
    administrator's payment-method mapping. No user ever types or picks a G/L:
    they choose a tender, and the account follows from configuration.

        CASH   -> SapCompanyMap.cash_gl_account (a drawer is not a house bank)
        UPI    -> the account mapped for UPI
        CHEQUE -> the account mapped for cheques; the payer's bank is a
                  separate field on the line and is sent as BankCode

    Raises BankMasterUnavailable when SAP cannot be reached and nothing is
    cached, and ValidationError when a mapping points at an account SAP no
    longer has — posting to a stale ledger is worse than failing loudly.
    """
    resolver = bank_master.PaymentAccountResolver(company)
    accounts = {'CASH': resolver.cash_gl()}

    methods = None
    if receipt is not None:
        methods = {m.method for m in receipt.methods.all()}

    for method in PaymentMethodEntry.Method.values:
        if method == PaymentMethodEntry.Method.CASH:
            continue
        if methods is not None and method not in methods:
            continue
        found = resolver.resolve(method)
        accounts[method] = found['gl_account'] if found else ''

    return accounts


def post_receipt_to_sap(receipt, user=None):
    """Post a receipt to SAP RIGHT NOW. Called on the final approval.

    Synchronous by design: the approver sees the real outcome — a DocEntry or
    SAP's own error — rather than a queued state they must chase.

    Called AFTER the approving transaction commits, deliberately. A SAP failure
    must not roll the approval back: the approval genuinely happened, and the
    document simply lands in PENDING_ERROR for the creator to correct.
    """
    from .sap_poster import post_document

    company_db = receipt.company_db or resolve_company_db(receipt.company)
    if receipt.company_db != company_db:
        receipt.company_db = company_db
        receipt.save(update_fields=['company_db', 'updated_at'])

    bank_accounts = _bank_accounts_for(receipt.company, receipt)

    # A cheque posts as a bank transfer, so it needs a collection account. If
    # none is mapped, fail HERE with a message naming the company — never fall
    # back to the cash G/L, which would park the money in a clearing account
    # that no deposit ever empties.
    methods = {m.method for m in receipt.methods.all()}
    if 'CHEQUE' in methods and not bank_accounts.get('CHEQUE'):
        raise ValidationError(
            f'No SAP collection bank is configured for CHEQUE payments for '
            f'{receipt.company}. Set it under payment method mappings before '
            f'posting.')

    payload = build_incoming_payment(
        receipt,
        bank_accounts=bank_accounts,
        bpl_id=resolve_bpl_id(receipt.company, receipt),
        # Resolved from NNM1 for the POSTING month — never the cheque date.
        series=hana_queries.fetch_incoming_payment_series(
            company=receipt.company, posting_date=receipt.payment_date),
    )
    return post_document(receipt, payload, user=user)


# ---------------------------------------------------------------------------
# Deposits
# ---------------------------------------------------------------------------

def validate_deposit(deposit):
    lines = list(deposit.lines.select_related('receipt'))
    if not lines:
        raise ValidationError('Select at least one payment to deposit.')

    # Only physically-carried tenders can be in a deposit: CASH and CHEQUE.
    # UPI arrives electronically, so there is nothing to hand over and no
    # deposit to record. One non-depositable line disqualifies the whole
    # receipt — a receipt is banked or it is not; it cannot be half-banked.
    #
    # Reads PaymentMethodEntry.DEPOSITABLE_METHODS, the same definition the
    # picker uses, so the two can never drift apart.
    not_depositable = sorted({
        entry.method
        for line in lines
        for entry in line.receipt.methods.all()
        if entry.method not in PaymentMethodEntry.DEPOSITABLE_METHODS
    })
    if not_depositable:
        raise ValidationError(
            f'Only cash and cheque receipts can be deposited. These payments '
            f'reached the bank electronically and are already accounted for: '
            f'{", ".join(not_depositable)}.')

    collected = sum((line.amount for line in lines), Decimal('0'))
    if collected != deposit.collected_amount:
        raise ValidationError(
            f'Selected payments total {collected} but collected_amount is '
            f'{deposit.collected_amount}.')
    if deposit.deposit_amount > collected:
        raise ValidationError('Deposit amount cannot exceed the collected amount.')
    if deposit.deposit_amount < collected and not deposit.shortfall_reason.strip():
        raise ValidationError('A reason is required when depositing less than collected.')

    # The deposit posts to THIS account's G/L (sap_payloads.build_deposit reads
    # bank_gl_account directly), so a blank one means SAP rejects the document
    # after it has already been approved.
    #
    # Reads the snapshot columns, not a FK: the bank master was removed and SAP
    # now owns the accounts, so the deposit carries its own copy.
    if not (deposit.bank_gl_account or '').strip():
        raise ValidationError(
            'This deposit has no SAP G/L account. Select the bank it was paid '
            'into before submitting.')
    return True


@transaction.atomic
def submit_deposit(deposit, user, ctx=None):
    if deposit.sap_doc_entry:
        raise ValidationError(
            f'{deposit.deposit_no} is already posted to SAP as DocEntry '
            f'{deposit.sap_doc_entry}. It cannot be submitted again.')
    # PENDING_ERROR is submittable — see submit_receipt.
    if deposit.status not in (BankDeposit.Status.DRAFT,
                              BankDeposit.Status.REJECTED,
                              BankDeposit.Status.PENDING_ERROR):
        raise ValidationError(
            f'A {deposit.get_status_display().lower()} deposit cannot be submitted.')

    validate_deposit(deposit)

    # Re-verify the bank against SAP at the LAST point before the approval
    # chain opens. SAP configuration can change between saving a draft and
    # submitting it, and an unverifiable bank must never enter the workflow.
    try:
        bank = bank_master.find_bank(deposit.company,
                                     deposit.bank_key or deposit.bank_gl_account)
    except bank_master.BankMasterUnavailable as exc:
        raise ValidationError(str(exc)) from exc
    if bank is None:
        raise ValidationError(
            'Select the bank this deposit was paid into before submitting.')
    # Re-snapshot: SAP may have renamed or re-pointed the account since.
    deposit.bank_key = bank['key']
    deposit.bank_code = bank['bank_code']
    deposit.bank_gl_account = bank['gl_account']
    deposit.bank_display_name = bank['label']
    deposit.save(update_fields=['bank_key', 'bank_code', 'bank_gl_account',
                                'bank_display_name', 'updated_at'])

    previous = deposit.status

    approval_services.submit(
        document=deposit,
        user=user,
        company=deposit.company,
        amount=deposit.deposit_amount,
        document_number=deposit.deposit_no,
        document_type='DEPOSIT',
        ctx=ctx,
    )
    deposit.status = BankDeposit.Status.PENDING_APPROVAL
    deposit.save(update_fields=['status', 'updated_at'])
    log_status(deposit, from_status=previous, to_status=deposit.status,
               user=user, reason='Submitted for approval.',
               ip=(ctx or {}).get('ip'))
    # Approval-required notification is fired by the engine's `submitted` hook
    # (payments.hooks), inside approval_services.submit's transaction above.
    return deposit


def sap_postable_amount(deposit):
    """The part of a deposit that still needs a SAP entry.

    CASH only. A cheque debited the bank when its RECEIPT posted (verified:
    DR bank / CR receivable), so the cheque is already in SAP; the OMS deposit
    records the physical hand-over and nothing more. Posting it again would
    debit the bank twice and credit a clearing account that never held it.

    Summed from the METHOD LINES, not from deposit.deposit_amount, because a
    mixed deposit's total covers cash and cheques together and only the cash
    share may reach SAP.
    """
    total = Decimal('0')
    for line in deposit.lines.select_related('receipt'):
        for entry in line.receipt.methods.all():
            if entry.method in PaymentMethodEntry.SAP_POSTABLE_DEPOSIT_METHODS:
                total += entry.amount
    return total


def post_deposit_to_sap(deposit, user=None):
    """Post a deposit to SAP RIGHT NOW. See post_receipt_to_sap.

    A cheque-only deposit posts NOTHING and is completed as an OMS record.
    """
    from .sap_poster import post_document

    company_db = deposit.company_db or resolve_company_db(deposit.company)
    if deposit.company_db != company_db:
        deposit.company_db = company_db
        deposit.save(update_fields=['company_db', 'updated_at'])

    # Cheque-only: there is no SAP document to create. Complete the OMS record
    # and leave sap_doc_entry / sap_doc_num / sap_trans_id NULL — fabricating
    # identifiers for a document that does not exist would be worse than
    # leaving the truth visible. `already_posted()` accepts status==POSTED on
    # its own, so this deposit is still protected from a second attempt.
    postable = sap_postable_amount(deposit)
    if postable <= 0:
        previous = deposit.status
        deposit.status = BankDeposit.Status.POSTED
        deposit.sap_posted_at = timezone.now()
        deposit.sap_response = (
            'Recorded in OMS. No SAP posting is required: the cheques in this '
            'deposit were already accounted for in SAP when their receipts '
            'posted.')
        deposit.save(update_fields=['status', 'sap_posted_at', 'sap_response',
                                    'updated_at'])
        log_status(deposit, from_status=previous, to_status=deposit.status,
                   user=user, actor_kind='SYSTEM',
                   reason='Cheque-only deposit — recorded in OMS, no SAP entry.')
        logger.info(
            'deposit %s completed without a SAP post (cheque-only)',
            deposit.deposit_no)
        return deposit

    # The former CheckKey precondition was removed with this change. It existed
    # because an ODPS deposit referenced cheques SAP already held, by CheckKey.
    # Cheques no longer reach SAP from a deposit at all, and sap_check_key is
    # permanently NULL now that receipts stop sending PaymentChecks — so the
    # guard would block every mixed deposit for a reason that no longer exists.

    # Mixed deposit: SAP receives the CASH share only. The OMS record keeps the
    # full physical amount (cash + cheques), which is what the employee
    # actually carried to the bank; the two figures answer different questions
    # and must not be reconciled into one.
    # The G/L being emptied. Same field the RECEIPTS debited
    # (bank_master.cash_gl -> SapCompanyMap.cash_gl_account), so the clearing
    # account provably nets to zero. Fail here rather than post a document
    # with a blank CardCode.
    source_gl = bank_master.PaymentAccountResolver(deposit.company).cash_gl()
    if not source_gl:
        raise ValidationError(
            f'No cash G/L is configured for {deposit.company}. Set it on the '
            f'company mapping before depositing.')

    payload = build_deposit(
        deposit,
        bpl_id=resolve_bpl_id(deposit.company),
        series=hana_queries.fetch_incoming_payment_series(
            company=deposit.company, posting_date=deposit.deposit_date),
        amount=postable,
        source_gl=source_gl,
    )
    return post_document(deposit, payload, user=user)
