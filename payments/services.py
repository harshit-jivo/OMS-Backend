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
               actor_kind='USER', reason='', ip=None):
    """Append a lifecycle row. Never updated after insert."""
    return PaymentStatusHistory.objects.create(
        content_type=ContentType.objects.get_for_model(document.__class__),
        object_id=document.pk,
        from_status=from_status or '',
        to_status=to_status,
        reason=reason or '',
        actor_kind=actor_kind,
        changed_by=user,
        changed_by_username=getattr(user, 'username', '') or '',
        ip_address=ip,
    )


def record_sap_history(receipt, *, action, status, response='',
                       doc_entry=None, doc_num=None, user=None,
                       attempt_number=None):
    """Append one SAP posting-history row. Never updates an existing one.

    `attempt_number` is derived rather than passed in by default: POST_STARTED
    opens a new attempt and every other action belongs to the attempt already in
    progress. Deriving it in one place keeps the numbering consistent no matter
    which code path logs the event.

    RESUBMITTED deliberately does NOT open an attempt. It is recorded at submit
    time, and the POST_STARTED that follows final approval is the attempt it
    leads to — numbering it separately would split one retry across two attempt
    numbers and make the timeline read as though a post had been skipped.
    """
    from .models import SapPostingHistory

    if attempt_number is None:
        last = (SapPostingHistory.objects
                .filter(payment=receipt)
                .order_by('-attempt_number')
                .values_list('attempt_number', flat=True)
                .first()) or 0
        opens = action == SapPostingHistory.Action.POST_STARTED
        attempt_number = last + 1 if opens else max(last, 1)

    return SapPostingHistory.objects.create(
        payment=receipt,
        attempt_number=attempt_number,
        action=action,
        status=status,
        sap_response=(response or '')[:4000],
        sap_doc_entry=doc_entry,
        sap_doc_num=doc_num,
        created_by=user,
        created_by_username=getattr(user, 'username', '') or '',
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


def resolve_bpl_id(company):
    """SAP branch for a company, or None when not configured.

    SAP rejects a document with "Specify an active branch" on multi-branch
    setups, so this must be sent when the company has one. Returning None (not
    0 or '') matters: an empty BPLID is itself invalid, so the payload builder
    omits the key entirely.
    """
    return _company_mapping(company).default_bpl_id


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
        from .models import SapPostingHistory

        record_sap_history(
            receipt,
            action=SapPostingHistory.Action.RESUBMITTED,
            status=SapPostingHistory.Status.POSTING,
            response='Payment resubmitted after correction.',
            user=user,
        )
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

    payload = build_incoming_payment(
        receipt,
        bank_accounts=_bank_accounts_for(receipt.company, receipt),
        bpl_id=resolve_bpl_id(receipt.company),
    )
    return post_document(receipt, payload, user=user)


# ---------------------------------------------------------------------------
# Deposits
# ---------------------------------------------------------------------------

def validate_deposit(deposit):
    lines = list(deposit.lines.select_related('receipt'))
    if not lines:
        raise ValidationError('Select at least one payment to deposit.')

    collected = sum((line.amount for line in lines), Decimal('0'))
    if collected != deposit.collected_amount:
        raise ValidationError(
            f'Selected payments total {collected} but collected_amount is '
            f'{deposit.collected_amount}.')
    if deposit.deposit_amount > collected:
        raise ValidationError('Deposit amount cannot exceed the collected amount.')
    if deposit.deposit_amount < collected and not deposit.shortfall_reason.strip():
        raise ValidationError('A reason is required when depositing less than collected.')

    # The deposit posts to THIS account's GL (sap_payloads.build_deposit reads
    # it directly), so a blank one means SAP rejects the document after it has
    # already been approved.
    account = deposit.bank_account
    if account and not (account.sap_gl_account or '').strip():
        raise ValidationError(
            f'The bank account "{account.name}" has no SAP GL account. '
            f'Ask an administrator to set one under Approval Management > '
            f'Masters > Bank accounts before depositing to it.')
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
    return deposit


def post_deposit_to_sap(deposit, user=None):
    """Post a deposit to SAP RIGHT NOW. See post_receipt_to_sap."""
    from .sap_poster import post_document

    company_db = deposit.company_db or resolve_company_db(deposit.company)
    if deposit.company_db != company_db:
        deposit.company_db = company_db
        deposit.save(update_fields=['company_db', 'updated_at'])

    # A cheque deposit references cheques SAP already holds, by CheckKey, which
    # only exists once the underlying receipt has posted. Refused here with an
    # explanation rather than posting a deposit with empty cheque lines.
    if deposit.deposit_type in (BankDeposit.DepositType.CHEQUE,
                                BankDeposit.DepositType.MIXED):
        unposted = [
            line.receipt.receipt_no
            for line in deposit.lines.select_related('receipt')
            for entry in line.receipt.methods.all()
            if entry.method == 'CHEQUE' and not entry.sap_check_key
        ]
        if unposted:
            raise ValidationError(
                f'These receipts must post to SAP before their cheques can be '
                f'deposited: {", ".join(sorted(set(unposted)))}.')

    payload = build_deposit(deposit, bpl_id=resolve_bpl_id(deposit.company))
    return post_document(deposit, payload, user=user)
