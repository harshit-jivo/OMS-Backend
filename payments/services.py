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

from . import workflow_flow

from . import bank_master, hana_queries
from . import sap_company
from .models import (
    BankDeposit,
    PaymentMethodEntry,
    PaymentReceipt,
    PaymentStatusHistory,
)
from .sap_payloads import build_deposit, build_incoming_payment

logger = logging.getLogger(__name__)


def log_status(document, *, to_status, from_status='', user=None,
               actor_kind='USER', reason='', ip=None,
               action=None, level=None, level_label='', stage_id=None,
               sap_doc_entry=None, sap_doc_num=None, change_data=None):
    """Append one row to the activity timeline. Never updated after insert.

    THE single history call for the payments module — approvals, edits and SAP
    attempts all land here, so one query renders the whole timeline.

    `action` says WHAT happened; the status pair says what it changed. When no
    action is given it falls back to STATUS_CHANGED, which keeps older callers
    working without claiming an event they did not record.

    `change_data` describes WHICH FIELDS an edit changed, and is accepted only
    on UPDATED. Every other action is a transition, not a field change, so a
    diff on one would be describing something that did not happen — it is
    dropped rather than stored, so no caller can quietly create that row.

    `actor_kind`, `ip`, `sap_doc_entry` and `sap_doc_num` are STILL ACCEPTED and
    deliberately ignored — their columns were dropped in migration 0031. Keeping
    the keywords means the dozen call sites that pass them (hooks.py,
    sap_poster.py, the test suite) keep working untouched; removing them would
    turn a schema cleanup into a rewrite of every writer, with a TypeError at
    each one that was missed. They can be deleted from the signature once the
    call sites are tidied, which is a separate, mechanical change.
    """
    resolved_action = action or PaymentStatusHistory.Action.STATUS_CHANGED
    if resolved_action != PaymentStatusHistory.Action.UPDATED:
        change_data = None
    return PaymentStatusHistory.objects.create(
        content_type=ContentType.objects.get_for_model(document.__class__),
        object_id=document.pk,
        action=resolved_action,
        from_status=from_status or '',
        to_status=to_status,
        reason=reason or '',
        level=level,
        level_label=level_label or '',
        # WHICH workflow stage this happened at, by reference. `level` above is
        # the rendered position; this survives a stage being renamed.
        stage_id=stage_id,
        changed_by_username=getattr(user, 'username', '') or '',
        change_data=change_data,
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
    """category -> SAP company DB, from the ENVIRONMENT.

    Delegates to the canonical resolver every other module already uses, so a
    payment and the invoice it settles can never disagree about which company
    they belong to. TEST and LIVE are separated by deployment configuration
    rather than by a row in a payments table.
    """
    return sap_company.resolve_company_db(company)


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
      3. the environment default branch, ONLY for an advance with no branch
         selected (legacy rows) and for a deposit, neither of which settles an
         invoice and so has no branch to inherit

    Raises ValidationError rather than guessing when invoices disagree, when a
    branch cannot be mapped, or when SAP cannot be reached: posting to the
    wrong ledger is worse than not posting.
    """
    if receipt is None:
        # A deposit: it settles no invoice and has no branch picker, so the
        # environment default is the only source. Unchanged behaviour — this
        # read the company mapping's default_bpl_id before.
        return sap_company.default_bpl_id(company)

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
        bpl = sap_company.default_bpl_id(company)
        logger.info('BPL resolve %s: advance, no branch selected -> BPLID %s '
                    '(source: environment default, legacy)',
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

    _validate_gl_accounts(receipt.company, {m.method for m in methods},
                          receipt=receipt)
    return True


def _validate_gl_accounts(company, methods_used, *, receipt=None):
    """Every method on the document must resolve to a SAP account.

    Without this the payload builder simply OMITS the account field and SAP
    rejects the document — after it has been fully approved, which is the worst
    possible moment to discover a configuration gap. Failing at submit puts the
    error in front of someone who can act on it.

    Resolved exactly as posting will resolve it (`_bank_accounts_for`), so a
    line carrying its own account needs no mapping at all, and only a keyless
    legacy line is checked against the mapping — including a mapping whose
    account has since been removed from SAP.
    """
    accounts = _bank_accounts_for(company, receipt)
    missing = sorted({m for m in methods_used if not accounts.get(m)})
    if not missing:
        return

    labels = dict(PaymentMethodEntry.Method.choices)
    names = ', '.join(labels.get(m, m) for m in missing)
    # Only a keyless legacy line can get here: a line with a snapshot always
    # names its account. So the remedy is to choose one — or, until the
    # mapping is retired, to configure it there.
    raise ValidationError(
        f'No receiving account is set for {names} in {company}. Edit the '
        f'payment and select the receiving account, or configure it under '
        f'Payments > Payment Method Mapping.')


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
    open_flow = workflow_flow.flow_of(receipt)
    if open_flow is not None and open_flow.is_open:
        raise ValidationError(
            f'{receipt.receipt_no} is still with its approver. They can retry '
            f'the SAP posting from their pending list once the problem is '
            f'fixed — it does not need resubmitting.')
    if receipt.status not in (PaymentReceipt.Status.DRAFT,
                              PaymentReceipt.Status.REJECTED,
                              PaymentReceipt.Status.PENDING_ERROR):
        raise ValidationError(
            f'A {receipt.get_status_display().lower()} receipt cannot be submitted.')

    # THE VERIFICATION GATE. Placed here, in the one function every path into
    # approval calls, so it cannot be bypassed by another route — the submit
    # endpoint, the verify endpoint and any future caller all pass through it.
    #
    # It sits AFTER the status check so the more specific message wins: a
    # posted receipt should be told it is posted, not that it needs verifying.
    #
    # Applies equally to the REJECTED and PENDING_ERROR retry paths. Those
    # receipts were verified before their first submission and stay VERIFIED
    # (verification is never rewound), so in practice this never blocks them —
    # but a receipt that somehow reaches those states unverified must not slip
    # into approval through the back door.
    if receipt.verification_status != PaymentReceipt.VerificationStatus.VERIFIED:
        raise ValidationError(
            'Payment must be verified before it can be submitted for approval.')

    validate_receipt(receipt)
    previous = receipt.status

    # Route through the Workflow Engine and open payments' own flow. Raises
    # when nothing routes it, which rolls this whole transaction back — a
    # document that cannot be approved must not sit in PENDING_APPROVAL.
    workflow_flow.start(receipt, user=user)

    receipt.status = PaymentReceipt.Status.PENDING_APPROVAL
    receipt.save(update_fields=['status', 'updated_at'])
    # PENDING_APPROVAL, not the STATUS_CHANGED default: entering the approval
    # chain is the business event, and an untagged row read as an anonymous
    # transition sitting between VERIFIED and APPROVED. The status assignment
    # above is untouched.
    # NOTE the history row for entering approval is written by
    # workflow_flow.start(), which knows the stage it landed on.

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
    # The "approval required" notification is fired by workflow_flow.start(),
    # inside this transaction — so it commits with the submission and rolls
    # back with it. No SAP is involved in this path.
    return receipt


def verify_receipt(receipt_id, user, *, remarks='', ctx=None):
    """Verify a receipt (the handover check) and send it into approval.

    ONE transaction, deliberately. Verification and submission must not come
    apart: a receipt marked VERIFIED that never entered the approval chain is
    invisible in both queues — gone from the verifier's list, absent from the
    approver's — and only a database read would find it. If the submission
    raises, the verification is rolled back with it and the receipt stays in
    the verifier's queue where somebody can act on it.

    Takes an ID rather than an instance: the row is re-read under
    `select_for_update()` inside the lock, so a caller cannot hand us a stale
    copy whose verification_status was read before another transaction changed
    it. This is the pattern `sap_poster.post_document` already uses to keep
    "one OMS receipt -> one SAP payment" true, applied to the same class of
    race.

    Returns the verified, submitted receipt.
    """
    with transaction.atomic():
        # The lock is held for the whole body — verification, history and
        # submission — so a second verifier blocks here and then reads the
        # committed VERIFIED state below rather than racing past it.
        receipt = (PaymentReceipt.objects
                   .select_for_update()
                   .get(pk=receipt_id))

        # Duplicate verification. Read from the LOCKED row, never from a copy
        # fetched before the lock: that is the whole point of re-reading here.
        if receipt.verification_status == PaymentReceipt.VerificationStatus.VERIFIED:
            who = (getattr(receipt.verified_by, 'name', None)
                   or getattr(receipt.verified_by, 'username', None)
                   or 'another user')
            raise ValidationError(
                f'{receipt.receipt_no} has already been verified by {who}.')

        # Separation of duties. The reason verification exists is to put a
        # second pair of eyes on physical money, so the creator verifying their
        # own entry would return the system to exactly the state it had before
        # this feature. `approvals` takes the same position on approval
        # (forbid_self_approval), and this mirrors it.
        if receipt.created_by_id == getattr(user, 'id', None):
            raise ValidationError('You cannot verify a payment you created.')

        # A receipt already in SAP is finished; verifying it would imply a
        # handover check on money that has already been posted and settled.
        if receipt.sap_doc_entry:
            raise ValidationError(
                f'{receipt.receipt_no} is already posted to SAP as DocEntry '
                f'{receipt.sap_doc_entry} and cannot be verified.')

        previous = receipt.verification_status
        receipt.verification_status = PaymentReceipt.VerificationStatus.VERIFIED
        receipt.verified_by = user
        receipt.verified_at = timezone.now()
        if remarks:
            receipt.verification_remarks = remarks
        receipt.save(update_fields=['verification_status', 'verified_by',
                                    'verified_at', 'verification_remarks',
                                    'updated_at'])

        # The audit trail is PaymentStatusHistory and nothing else — the model
        # columns above are a queryable projection of this row. The status pair
        # records the VERIFICATION axis, which is what actually changed; the
        # main status is still DRAFT at this point and is moved by the submit
        # below, which logs its own row.
        log_status(receipt, from_status=previous,
                   to_status=receipt.verification_status,
                   user=user,
                   action=PaymentStatusHistory.Action.VERIFIED,
                   reason=remarks or 'Payment verified.',
                   ip=(ctx or {}).get('ip'))

        # Into the EXISTING chain, through the one entry point. The guard in
        # submit_receipt now passes because the row above is VERIFIED within
        # this transaction. Nothing about the approval engine changes.
        submit_receipt(receipt, user, ctx=ctx)

    return receipt


#: The receiving-account snapshot columns on PaymentMethodEntry. All blank on
#: a line entered before the account picker existed (migration 0035 added them
#: with DEFAULT ''), and never blank on one entered with it.
_SNAPSHOT_FIELDS = ('account_key', 'gl_account', 'bank_code',
                    'receiving_bank_name', 'account_number', 'branch')


def snapshot_gl_account(entry):
    """The G/L this line's own snapshot names, or None for a legacy line.

    Three outcomes, deliberately distinct:

      * every snapshot column blank  -> None. A LEGACY line, entered before the
        user chose accounts; the caller falls back to the method mapping.
      * a complete, self-consistent snapshot -> its `gl_account`.
      * anything in between -> ValidationError. A half-written or contradictory
        snapshot is never "close enough", and falling back to the mapping would
        post the money somewhere the user did not choose.

    "Self-consistent" is checked against the key the account was chosen by:
    a cash line's key IS its G/L; a banked line's key is BANKCODE:GLACCOUNT.
    The check needs nothing but the row itself — it never asks SAP or the
    mapping, which is what makes the snapshot authoritative after either
    changes.
    """
    values = {f: (getattr(entry, f, '') or '').strip() for f in _SNAPSHOT_FIELDS}
    if not any(values.values()):
        return None

    key, gl, bank_code = (values['account_key'], values['gl_account'],
                          values['bank_code'])
    if entry.method == PaymentMethodEntry.Method.CASH:
        consistent = bool(key and gl and key == gl)
    else:
        consistent = bool(key and gl and bank_code
                          and key == f'{bank_code}:{gl}')
    if not consistent:
        raise ValidationError(
            f'The receiving account stored on this {entry.method} payment is '
            f'incomplete or inconsistent (account {key or "-"}, G/L '
            f'{gl or "-"}). Edit the payment and select the receiving account '
            f'again.')
    return gl


def _bank_accounts_for(company, receipt=None):
    """method -> SAP G/L account, for payload building.

    THE ACCOUNT IS THE ONE THE COLLECTOR CHOSE, frozen on the line when the
    payment was saved. That is what posts, regardless of what the bank master
    or any configuration says now: re-resolving it here would let a later edit
    move money the user already directed elsewhere.

    It used to fall back to `PaymentMethodMapping` — one admin-configured
    account per method per company — for lines raised before the picker
    existed. That table has been retired: every line that could still be
    posted or deposited was stamped with the account the mapping would have
    chosen (`backfill_receiving_accounts`), so the fallback had nothing left
    to answer.

    A line with no account is therefore a real gap now, not a legacy shape. It
    is reported by `validate_accounts_available` at submit — in front of
    someone who can fix it by opening the receipt and choosing an account —
    rather than guessed at here.

    Per method, because SAP holds one CashAccount and one TransferAccount per
    document: every line of a method must name the same account, and a method
    whose lines disagree is refused rather than merged.

    Raises ValidationError on an inconsistent or ambiguous method.
    """
    if receipt is None:
        return {}

    by_method = {}
    for entry in receipt.methods.all():
        by_method.setdefault(entry.method, []).append(entry)

    accounts = {}
    for method, entries in by_method.items():
        snapshots = [snapshot_gl_account(entry) for entry in entries]
        if all(gl is None for gl in snapshots):
            # No account on any line of this method. Left out of the result so
            # `validate_accounts_available` names it.
            continue
        if any(gl is None for gl in snapshots):
            raise ValidationError(
                f'This receipt has {method} lines both with and without a '
                f'receiving account. Edit it and select the account for every '
                f'line.')
        if len(set(snapshots)) > 1:
            raise ValidationError(
                f'This receipt has {method} lines received into different '
                f'accounts ({", ".join(sorted(set(snapshots)))}). SAP allows '
                f'one account per method on a payment, so split it into '
                f'separate receipts.')
        accounts[method] = snapshots[0]

    return accounts

def cash_sources_for_receipts(company, receipts):
    """The DISTINCT cash G/Ls a set of receipts would empty, sorted.

    One entry means the deposit clears one drawer and can post. Several means
    the user picked receipts from different drawers: SAP takes one source
    account per deposit document, and merging them — or silently picking one —
    would credit a drawer that never held the money. The caller refuses and
    names both accounts so the user can split the deposit.

    Read per cash line from the account the collector chose. A line with no
    account contributes nothing; it used to fall back to the CASH method
    mapping, which has been retired.

    Cheque lines contribute nothing: a cheque reached the bank when its own
    receipt posted, so a deposit re-posting it would double-debit. An empty
    result therefore means "no cash to post" — a cheque-only deposit.

    Blank is not a source: a line with no account is reported by the caller as
    "no cash G/L configured", the message it has always used.
    """
    sources = set()
    for receipt in receipts:
        for entry in receipt.methods.all():
            if entry.method not in PaymentMethodEntry.SAP_POSTABLE_DEPOSIT_METHODS:
                continue
            # The drawer the line itself names. A line with none contributes
            # nothing, and the caller reports "no cash G/L" — the same message
            # it gave when the mapping could not answer either.
            gl = snapshot_gl_account(entry)
            if (gl or '').strip():
                sources.add(gl.strip())
    return sorted(sources)


def deposit_cash_sources(deposit):
    """`cash_sources_for_receipts` for the receipts already on a deposit."""
    receipts = [line.receipt
                for line in deposit.lines.select_related('receipt')]
    return cash_sources_for_receipts(deposit.company, receipts)


def freeze_deposit_source(deposit):
    """Set (and return) the drawer this deposit empties. Does not save.

    Called from submit, beside the destination-bank re-snapshot. Separate from
    it so the rule can be tested without a database: submit runs inside a
    transaction, and this is the part with the accounting in it.
    """
    deposit.source_gl_account = check_one_cash_source(
        deposit_cash_sources(deposit))
    return deposit.source_gl_account


def check_one_cash_source(sources):
    """Refuse a deposit that spans more than one cash account.

    Shared by the serializer (so the error arrives while the user is still
    choosing receipts) and by validate_deposit (so no other path can get past
    it). Returns the single source, or '' when there is no cash at all.
    """
    if len(sources) > 1:
        raise ValidationError(
            f'Cannot create this deposit because the selected receipts belong '
            f'to different CASH SALE accounts ({" and ".join(sources)}). '
            f'Please create separate deposits for each CASH SALE account.')
    return sources[0] if sources else ''


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

def cash_total_for_receipts(receipts):
    """The CASH in these receipts — the only money a deposit actually moves.

    A cheque in the same receipt is deliberately ignored. It debited the bank
    when the RECEIPT posted (Dr bank / Cr receivable), so its value is already
    in SAP; the deposit records the day the paper was carried in and nothing
    more. Summing cash and cheques together would give a figure that is not
    the SAP posting, not the shortfall base, and not anything countable.

    Reads `SAP_POSTABLE_DEPOSIT_METHODS`, the same definition the payload
    builder uses, so the two cannot drift apart.
    """
    total = Decimal('0')
    for receipt in receipts:
        for entry in receipt.methods.all():
            if entry.method in PaymentMethodEntry.SAP_POSTABLE_DEPOSIT_METHODS:
                total += entry.amount
    return total


def cash_total_for_lines(lines):
    """`cash_total_for_receipts` for deposit lines. See it for the reasoning."""
    return cash_total_for_receipts([line.receipt for line in lines])


def derive_deposit_type(cash_total):
    """What a deposit IS, read from its contents rather than from a picker.

    There is no MIXED any more (see BankDeposit.DepositType): with the
    arithmetic cash-only, a deposit either moves cash or it moves none, and a
    cheque riding along on a cash deposit is a record, not a second kind.
    """
    return (BankDeposit.DepositType.CASH if cash_total > 0
            else BankDeposit.DepositType.CHEQUE)


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

    # CASH ONLY. The cheques in these receipts are not added in: each one
    # reached the bank when its own receipt posted to SAP, so its value is
    # already accounted for and this deposit only records the day the paper
    # was handed over. Adding it here would produce a total that matches
    # neither the SAP document nor the notes in the employee's hand.
    collected = cash_total_for_lines(lines)
    if collected != deposit.collected_amount:
        raise ValidationError(
            f'Selected payments hold {collected} in cash but collected_amount '
            f'is {deposit.collected_amount}.')
    if deposit.deposit_amount > collected:
        raise ValidationError(
            f'Cannot bank {deposit.deposit_amount}; these payments hold only '
            f'{collected} in cash.')
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

    # One cash drawer per deposit. Checked here as well as in the serializer
    # because submit is the last point before the approval chain opens, and
    # the receipts can be edited after the deposit was first saved.
    check_one_cash_source(deposit_cash_sources(deposit))
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

    # Freeze the SOURCE — the drawer being emptied — beside the DESTINATION
    # bank above. From here the deposit posts the account its receipts were
    # actually received into, whatever the mapping is changed to afterwards.
    # Blank for a cheque-only deposit, which posts nothing.
    freeze_deposit_source(deposit)

    deposit.save(update_fields=['bank_key', 'bank_code', 'bank_gl_account',
                                'bank_display_name', 'source_gl_account',
                                'updated_at'])

    previous = deposit.status

    workflow_flow.start(deposit, user=user)
    deposit.status = BankDeposit.Status.PENDING_APPROVAL
    deposit.save(update_fields=['status', 'updated_at'])
    log_status(deposit, from_status=previous, to_status=deposit.status,
               user=user, reason='Submitted for approval.',
               ip=(ctx or {}).get('ip'))
    # Approval-required notification is fired by the engine's `submitted` hook
    # (payments.hooks), inside approval_services.submit's transaction above.
    return deposit


def sap_postable_amount(deposit):
    """The part of a deposit that still needs a SAP entry: the cash banked.

    This is `deposit_amount` ITSELF, with no arithmetic on top, because
    `deposit_amount` now means "cash actually paid in" and nothing else. The
    SAP document and this field are the same number by construction, which is
    the whole point: what SAP says left the drawer is what the employee says
    they handed over.

    A cheque never appears here. It debited the bank when its RECEIPT posted
    (verified: DR bank / CR receivable), so it is already in SAP; the deposit
    records the day the paper was carried in. Posting it again would debit the
    bank twice and credit a clearing account that never held it.

    WHY NOT THE METHOD LINES. This used to sum the CASH entries of the linked
    receipts and ignore `deposit_amount` entirely. That is right only when the
    full collection is banked. The AP team routinely spends part of a
    collection before it reaches the bank — which is what `shortfall_reason`
    is for — and the old sum posted the whole cash share regardless. Observed
    on DEP-OIL-20260919-000002: 618,000 credited out of the drawer in SAP
    against 2,091 actually banked, overstating the emptying by 615,909.

    Zero is a legitimate answer, for a cheque-only deposit or one where every
    rupee of cash was spent; the caller routes that to the no-SAP-needed path.
    """
    return deposit.deposit_amount


def post_deposit_to_sap(deposit, user=None):
    """Post a deposit to SAP RIGHT NOW. See post_receipt_to_sap.

    A cheque-only deposit posts NOTHING and is completed as an OMS record.
    """
    from .sap_poster import post_document

    company_db = deposit.company_db or resolve_company_db(deposit.company)
    if deposit.company_db != company_db:
        deposit.company_db = company_db
        deposit.save(update_fields=['company_db', 'updated_at'])

    # No cash banked: there is no SAP document to create. Either the deposit
    # carries cheques only, or every rupee of its cash was spent before it
    # reached the bank (a full shortfall). Complete the OMS record
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
            'Recorded in OMS. No SAP posting is required: this deposit banked '
            'no cash. Any cheques it carries were already accounted for in '
            'SAP when their receipts posted.')
        deposit.save(update_fields=['status', 'sap_posted_at', 'sap_response',
                                    'updated_at'])
        log_status(deposit, from_status=previous, to_status=deposit.status,
                   user=user, actor_kind='SYSTEM',
                   reason='Cheque-only deposit — recorded in OMS, no SAP entry.')

        # COMPLETE THE APPROVAL TOO. The final approval leaves the flow at its
        # last stage until a SAP post succeeds, and this deposit will never
        # make one — there is nothing for SAP to record. Without this the
        # deposit would read POSTED while its flow sat waiting for an approver
        # forever. "SAP accepted it" and "SAP was not needed" are different
        # reasons to be finished, and both finish it.
        from . import workflow_flow
        workflow_flow.complete_after_sap(deposit)

        logger.info(
            'deposit %s completed without a SAP post (cheque-only)',
            deposit.deposit_no)
        return deposit

    # The former CheckKey precondition was removed with this change. It existed
    # because an ODPS deposit referenced cheques SAP already held, by CheckKey.
    # Cheques no longer reach SAP from a deposit at all, and sap_check_key is
    # permanently NULL now that receipts stop sending PaymentChecks — so the
    # guard would block every mixed deposit for a reason that no longer exists.

    # SAP receives the cash that was banked — `deposit_amount`, exactly. Any
    # cheques on this deposit are recorded beside it and contribute nothing:
    # they were posted under their own receipts.
    # The G/L being emptied — the same account the RECEIPTS debited, so the
    # clearing account provably nets to zero. Verified against live SAP:
    # deposits 21977 and 21963 posted Dr 2201102 / Cr 1105001 with 1105001 as
    # the CardCode. Fail here rather than post a document with a blank one.
    # The account frozen at submit, which is the one its receipts were
    # received into. Never re-resolved: an administrator's later edit must not
    # move money out of a drawer that never held it.
    #
    # Deposits raised before the freeze had theirs written from their own
    # receipts by `backfill_receiving_accounts`, so none is left without one.
    source_gl = (deposit.source_gl_account or '').strip()
    if not source_gl:
        raise ValidationError(
            f'This deposit does not record which cash account it empties. '
            f'Select the receiving account on its receipts and submit it '
            f'again before depositing.')

    payload = build_deposit(
        deposit,
        bpl_id=resolve_bpl_id(deposit.company),
        series=hana_queries.fetch_incoming_payment_series(
            company=deposit.company, posting_date=deposit.deposit_date),
        amount=postable,
        source_gl=source_gl,
    )
    return post_document(deposit, payload, user=user)
