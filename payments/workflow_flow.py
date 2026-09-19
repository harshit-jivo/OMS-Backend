"""Payments' own approval runtime, over the generic Workflow Engine.

The engine answers ONE question — which workflow applies to this document, and
what are its stages — and writes nothing. Everything after that is payments':
its flow rows, its approve/reject/cancel/resubmission rules, its history, its
notifications and its SAP posting. See
`docs/Approvals/WORKFLOW_MODULE_INTEGRATION.md` §§6, 9, 11.

SELECTION IS KEYED ON THE DOCUMENT NUMBER
-----------------------------------------
Payments registers ONE module for two document types, and receipt ids and
deposit ids overlap — receipt 5 and deposit 5 both exist. Keying selection on
`id` would let a deposit's query match a receipt and produce
`AmbiguousWorkflowSelection` on an ordinary submit. Document numbers cannot
collide (`RCP-…` / `DEP-…`, both unique), so every payments workflow query must
project them under the common alias `doc_no`:

    SELECT *, receipt_no AS doc_no FROM payment_receipt
    SELECT *, deposit_no AS doc_no FROM payment_bank_deposit

and this module passes `key_column='doc_no'`.

THE LEADING `SELECT *` IS REQUIRED. A query is validated when it is CONFIGURED,
and validation cannot know the runtime key column — the module supplies that at
submit time — so it checks the engine's default, `id`
(workflow/services/conditions.py:check_query). A projection naming only
`doc_no` is refused at configuration and can never be stamped as valid, so it
would never take part in routing.

WHO MAY ACT IS NEVER STORED
---------------------------
`flow.current_stage_id` is the engine's stage id; responsibility is resolved
from it on every read through `get_stage_assignment`, so reassigning a stage or
starting a replacement re-routes documents already waiting with nothing here
updated. `flow.current_user` is a denormalised convenience for lists only.

THE FINAL APPROVAL IS NOT COMPLETE UNTIL SAP ACCEPTS THE DOCUMENT
-----------------------------------------------------------------
Approving the last stage is an AUTHORISATION, not a completion. The flow stays
PENDING at that final stage and only becomes APPROVED when SAP has actually
accepted the posting (`complete_after_sap`). Until then the document is owed a
SAP post and its approver still holds it.

This is deliberate, and it is the difference from the PRDO lifecycle this
module started from. There, the final approval completed the flow immediately
and a SAP failure *reopened* it. That left two holes:

  * between the commit and the SAP call the flow read as APPROVED while nothing
    had been posted — a completed approval for a document that does not exist
    in SAP;
  * `transaction.on_commit` is not durable. A restart between the commit and
    the callback lost the only thing that would ever post the document, and it
    stranded: flow APPROVED, document PENDING_APPROVAL, no SAP call ever made.

Keeping the flow at the final stage closes both. A lost callback now leaves the
document exactly where its approver can retry it, which is where it belongs.

SAP is still called AFTER the transaction commits. A 5-7 second call must not
hold the approval's row locks, and the document carries POSTING_TO_SAP across
that boundary so the state is durable rather than living only in a callback.

    final approve -> POSTING_TO_SAP, flow still PENDING at the final stage
        SAP accepts  -> document POSTED,         flow APPROVED (complete)
        SAP refuses  -> document PENDING_ERROR,  flow PENDING at the final
                        stage; the same approver retries
        SAP silent   -> document SAP_UNKNOWN,    flow untouched and the retry
                        REFUSED, because the document may already exist there

History is appended for every one of those steps and never deleted.
"""
import logging

from django.core.exceptions import ValidationError
from django.db import transaction

from workflow.exceptions import WorkflowError
from workflow.services import selection
from workflow.services.assignments import get_stage_assignment

from .apps import MODULE_CODE
from .models import (BankDeposit, BankDepositFlow, FlowStatus,
                     PaymentReceipt, PaymentReceiptFlow, PaymentStatusHistory)

logger = logging.getLogger(__name__)

#: The alias every payments workflow query must project. See the module
#: docstring — this is what makes one module row serve two document types.
KEY_COLUMN = 'doc_no'


class PaymentFlowError(ValidationError):
    """A payments approval could not proceed. Carries an operator message."""


# ---------------------------------------------------------------------------
# Document shape — the two document types differ in three details only
# ---------------------------------------------------------------------------

class DocumentKind:
    """The two BUSINESS workflows payments runs, as an explicit value.

    Receipts and deposits share this module, this engine and every helper in
    it, but they are separate business workflows and must never route into one
    another. Naming the kind makes that separation something the code states
    and checks, rather than something that merely happens to hold because the
    document numbers are prefixed differently today.
    """

    RECEIPT = 'RECEIPT'
    DEPOSIT = 'DEPOSIT'


#: The document-number prefix each kind uses. A CONVENTION, and deliberately
#: not the thing routing depends on — see `_guard_routing`.
KIND_PREFIX = {
    DocumentKind.RECEIPT: 'RCP-',
    DocumentKind.DEPOSIT: 'DEP-',
}


def kind_of(document):
    """RECEIPT or DEPOSIT — decided by the model, not by the number."""
    return (DocumentKind.RECEIPT if isinstance(document, PaymentReceipt)
            else DocumentKind.DEPOSIT)


def source_table_for(kind):
    """The table a workflow query must read to serve this kind."""
    return (PaymentReceipt._meta.db_table if kind == DocumentKind.RECEIPT
            else BankDeposit._meta.db_table)


def kinds_a_query_serves(query_text):
    """Which business workflow(s) a configured query targets.

    Read from the TABLE the query selects from, which is the only thing that
    decides which documents it can ever return. A query naming both tables
    serves both, and that is a configuration fault this module refuses to route
    through — see `_guard_routing`.
    """
    text = (query_text or '').lower()
    return {kind for kind in (DocumentKind.RECEIPT, DocumentKind.DEPOSIT)
            if source_table_for(kind).lower() in text}


def flow_model_for(document):
    return (PaymentReceiptFlow if isinstance(document, PaymentReceipt)
            else BankDepositFlow)


def document_number(document):
    """The value workflow selection keys on."""
    return (document.receipt_no if isinstance(document, PaymentReceipt)
            else document.deposit_no)


def _status_enum(document):
    return (PaymentReceipt.Status if isinstance(document, PaymentReceipt)
            else BankDeposit.Status)


def _document_of(flow):
    return flow.receipt if isinstance(flow, PaymentReceiptFlow) else flow.deposit


def flow_of(document):
    """The document's flow, or None when it has never been submitted."""
    return getattr(document, 'flow', None)


# ---------------------------------------------------------------------------
# Helpers — identical in shape to backdate/production
# ---------------------------------------------------------------------------

def effective_user_id(stage_id):
    """Who may act at this stage TODAY, after replacements. None if unknown."""
    if not stage_id:
        return None
    assignment = get_stage_assignment(stage_id)
    return assignment.effective_user_id if assignment else None


def _point_at(flow, stage_id):
    """Move the flow to one stage and refresh its denormalised user."""
    flow.current_stage_id = stage_id
    flow.current_user_id = effective_user_id(stage_id)


def stages_for(flow):
    """The workflow's stages as configured RIGHT NOW.

    Re-read rather than remembered, so a stage deactivated mid-approval is
    skipped instead of deadlocking the document.
    """
    return selection.stages_for(flow.workflow)


def _locked(flow):
    model = type(flow)
    related = 'receipt' if model is PaymentReceiptFlow else 'deposit'
    return (model.objects.select_for_update()
            .select_related(related, 'workflow').get(pk=flow.pk))


def _guard(flow):
    if flow.status != FlowStatus.PENDING:
        raise PaymentFlowError(
            f'This document is already {flow.get_status_display().lower()}.')
    if not flow.current_stage_id:
        raise PaymentFlowError('This document is not waiting at any stage.')


#: Statuses in which a document must never be handed to SAP again. Each one
#: means a posting either succeeded or MIGHT have succeeded, and a second post
#: would take the customer's money twice.
UNPOSTABLE_STATUSES = frozenset({
    'POSTED',            # it is in SAP
    'POSTING_TO_SAP',    # a call is in flight right now
    'SAP_UNKNOWN',       # SAP never answered; it may hold the document
    'CANCELLED_IN_SAP',  # it was in SAP and was cancelled there
    'CANCELLED',         # withdrawn by the submitter
})


def _locked_document(flow):
    """Re-read the document under a row lock, for the final-stage decision.

    The flow row is locked first (`_locked`), so two approvals of the same
    document serialise here. That ordering is what makes a double post
    impossible: the second approval cannot read the document's status until the
    first has committed POSTING_TO_SAP, and `_guard_postable` then refuses it.
    """
    document = _document_of(flow)
    return (type(document).objects.select_for_update().get(pk=document.pk))


def _guard_postable(document):
    """Refuse a final approval that would post a document to SAP twice.

    This is the guard the old lifecycle did not need: completing the flow at
    the moment of approval made a second approval structurally impossible.
    Now that the flow deliberately stays at the final stage so it can be
    retried, the document's own SAP state is what says whether a retry is
    legitimate.
    """
    if document.sap_doc_entry:
        raise PaymentFlowError(
            f'This document is already posted to SAP as DocEntry '
            f'{document.sap_doc_entry}. It cannot be posted again.')
    if document.status in UNPOSTABLE_STATUSES:
        if document.status == 'POSTING_TO_SAP':
            raise PaymentFlowError(
                'A SAP posting for this document is already in progress. '
                'Wait for it to finish before trying again.')
        if document.status == 'SAP_UNKNOWN':
            raise PaymentFlowError(
                'SAP did not answer the last posting, so it is not yet known '
                'whether this document was created there. It must be verified '
                'before it can be posted again.')
        raise PaymentFlowError(
            f'A {document.get_status_display().lower()} document cannot be '
            f'posted to SAP.')


def _log(document, *, action, user, stage_id=None, reason='', to_status=None,
         from_status='', sequence=None, total=None):
    """Append one history row. Payments' log is append-only; nothing edits it."""
    from .services import log_status

    label = ''
    if sequence and total:
        label = f'Stage {sequence} of {total}'
    return log_status(
        document, from_status=from_status,
        to_status=to_status if to_status is not None else document.status,
        user=user, action=action, reason=reason, level=sequence,
        level_label=label, stage_id=stage_id)


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

@transaction.atomic
def start(document, *, user, remarks=''):
    """Route a document and open its flow at the first stage.

    Caller must already be inside the submitting transaction: a document that
    cannot be routed must not be left in PENDING_APPROVAL, which is why every
    failure here raises and takes the status change with it.

    Re-submission of a REJECTED document reuses the SAME flow row and
    re-selects against the CURRENT configuration (§11) — the engine keeps no
    resubmission state, and payments decides whether it is allowed.
    """
    model = flow_model_for(document)
    existing = flow_of(document)
    if existing is not None and existing.status == FlowStatus.PENDING:
        raise PaymentFlowError(
            'This document is already waiting for approval.')

    try:
        chosen = selection.select_for_module(
            module_code=MODULE_CODE,
            document_id=document_number(document),
            company=document.company,
            key_column=KEY_COLUMN,
        )
    except WorkflowError as exc:
        # The engine's messages are written for an operator and carry no SQL or
        # connection detail, so they pass straight through.
        raise PaymentFlowError(str(exc)) from exc

    _guard_routing(document, chosen)

    if not chosen.stages:
        raise PaymentFlowError(
            f'Workflow "{chosen.workflow.code}" has no active stages '
            f'configured, so this document cannot be approved by anyone.')

    first = chosen.stages[0]
    flow = existing or model(**{
        'receipt' if model is PaymentReceiptFlow else 'deposit': document})
    flow.status = FlowStatus.PENDING
    flow.workflow = chosen.workflow
    flow.total_stage = len(chosen.stages)
    _point_at(flow, first.id)
    flow.save()

    resubmitted = existing is not None
    _log(document,
         action=(PaymentStatusHistory.Action.RESUBMITTED if resubmitted
                 else PaymentStatusHistory.Action.PENDING_APPROVAL),
         user=user, stage_id=first.id, reason=remarks,
         sequence=first.sequence, total=flow.total_stage)

    from . import notification_events
    notification_events.publish_stage_awaiting(document, flow, user)
    return flow



def _guard_routing(document, chosen):
    """Refuse a workflow that does not belong to this document's kind.

    THE SEPARATION BETWEEN THE RECEIPT AND DEPOSIT WORKFLOWS IS ENFORCED HERE.

    Selection is driven by configuration: the engine runs every active query
    for the module and takes the workflow whose query matched. That makes the
    prefixes `RCP-` / `DEP-` the only thing keeping a receipt out of the
    deposit workflow — a CONVENTION in data, not a rule in code. One query
    written against the wrong table, or one document numbered by hand, and a
    receipt could be approved by the deposit chain, posted down the deposit
    path, and no code would object.

    So the match is checked against the table the query actually reads. A
    receipt may only ever be routed by a query that selects from the receipt
    table, and a deposit by one that selects from the deposit table. A query
    naming both is refused outright rather than guessed at: it could return
    either kind, and which one it meant is not something this module may
    decide on an administrator's behalf.

    This is payments' own rule and lives here. The engine is generic and has
    no business reason to know that these two document types must not mix.
    """
    kind = kind_of(document)
    query = getattr(chosen, 'matched_query', None)
    served = kinds_a_query_serves(getattr(query, 'query_text', ''))
    name = getattr(query, 'name', '') or '(unnamed)'
    code = chosen.workflow.code

    if served == {kind}:
        return

    if not served:
        raise PaymentFlowError(
            f'Workflow "{code}" matched this {kind.lower()} through query '
            f'"{name}", which does not read the {kind.lower()} table. The '
            f'{kind.lower()} and the other payments document type must be '
            f'routed by separate queries; ask a workflow administrator to '
            f'check it.')

    if len(served) > 1:
        raise PaymentFlowError(
            f'Query "{name}" on workflow "{code}" reads both the receipt and '
            f'the deposit tables, so it cannot say which of the two workflows '
            f'this document belongs to. Receipts and deposits must be routed '
            f'by separate queries.')

    other = next(iter(served))
    raise PaymentFlowError(
        f'This {kind.lower()} matched workflow "{code}", which is configured '
        f'for {other.lower()}s through query "{name}". A {kind.lower()} must '
        f'not be approved by the {other.lower()} workflow; ask a workflow '
        f'administrator to check the query configuration.')

# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

@transaction.atomic
def approve(flow, *, user, remarks=''):
    """Approve the stage this document is waiting at.

    One approve completes a stage (§9 — there is no quorum). The last one
    completes the document and queues the SAP post for AFTER the commit.
    """
    flow = _locked(flow)
    _guard(flow)
    document = _document_of(flow)

    decided_stage_id = flow.current_stage_id
    stages = stages_for(flow)
    current = next((s for s in stages if s.id == decided_stage_id), None)
    following = ([s for s in stages if s.sequence > current.sequence]
                 if current else [])

    _log(document, action=PaymentStatusHistory.Action.APPROVED, user=user,
         stage_id=decided_stage_id, reason=remarks,
         sequence=current.sequence if current else None,
         total=flow.total_stage)

    from . import notification_events

    if following:
        _point_at(flow, following[0].id)
        flow.save(update_fields=['current_stage', 'current_user', 'updated_at'])
        notification_events.publish_stage_awaiting(document, flow, user)
        return flow

    # THE FINAL STAGE. The approval is an authorisation; it does not complete
    # the flow. See the module docstring: the flow stays PENDING at this stage
    # until SAP accepts the document, so a refusal or a lost callback leaves it
    # with the approver who can retry rather than completed-but-unposted.
    document = _locked_document(flow)
    _guard_postable(document)

    statuses = _status_enum(document)
    document.status = statuses.POSTING_TO_SAP
    document.save(update_fields=['status', 'updated_at'])

    # The stage is unchanged; only the effective user is refreshed, because a
    # replacement may have started since the document arrived here.
    _point_at(flow, decided_stage_id)
    flow.save(update_fields=['current_stage', 'current_user', 'updated_at'])

    _queue_sap_post(document, user)
    notification_events_decision(document, approved=True, actor=user)
    return flow


@transaction.atomic
def reject(flow, *, user, remarks):
    """Reject at the current stage. Terminal for this execution (§9)."""
    if not (remarks or '').strip():
        raise PaymentFlowError('A reason is required when rejecting.')

    flow = _locked(flow)
    _guard(flow)
    document = _document_of(flow)
    statuses = _status_enum(document)
    previous = document.status

    _log(document, action=PaymentStatusHistory.Action.REJECTED, user=user,
         stage_id=flow.current_stage_id, reason=remarks,
         from_status=previous, to_status=statuses.REJECTED)

    document.status = statuses.REJECTED
    document.save(update_fields=['status', 'updated_at'])

    flow.status = FlowStatus.REJECTED
    _point_at(flow, None)
    flow.save(update_fields=['status', 'current_stage', 'current_user',
                             'updated_at'])

    notification_events_decision(document, approved=False, actor=user,
                                 reason=remarks)
    return flow


@transaction.atomic
def cancel(flow, *, user, remarks=''):
    """Withdraw a document from approval. The submitter's own escape hatch."""
    flow = _locked(flow)
    _guard(flow)
    document = _document_of(flow)
    statuses = _status_enum(document)
    previous = document.status

    _log(document, action=PaymentStatusHistory.Action.CANCELLED, user=user,
         stage_id=flow.current_stage_id,
         reason=remarks or 'Cancelled by submitter.',
         from_status=previous, to_status=statuses.CANCELLED)

    document.status = statuses.CANCELLED
    document.save(update_fields=['status', 'updated_at'])

    flow.status = FlowStatus.CANCELLED
    _point_at(flow, None)
    flow.save(update_fields=['status', 'current_stage', 'current_user',
                             'updated_at'])
    return flow


# ---------------------------------------------------------------------------
# SAP
# ---------------------------------------------------------------------------

def _queue_sap_post(document, user):
    """Post to SAP AFTER this transaction commits (the PRDO lifecycle).

    A 5-7 second SAP call must not hold the approval's row locks, and a SAP
    failure must not undo an approval that genuinely happened.
    """
    from .services import post_deposit_to_sap, post_receipt_to_sap

    if isinstance(document, PaymentReceipt):
        transaction.on_commit(lambda: post_receipt_to_sap(document, user=user))
    else:
        transaction.on_commit(lambda: post_deposit_to_sap(document, user=user))


def is_at_final_stage(flow):
    """True when the flow is waiting at the LAST stage of its workflow.

    Read from the workflow's stages as configured right now, for the same
    reason `stages_for` re-reads them: a stage added or deactivated after the
    document arrived changes which stage is last, and the retry rule has to
    follow the configuration rather than a remembered count.
    """
    if flow is None or not flow.current_stage_id:
        return False
    stages = stages_for(flow)
    return bool(stages) and stages[-1].id == flow.current_stage_id


@transaction.atomic
def complete_after_sap(document):
    """Complete the flow — SAP has accepted the document.

    THE ONLY PLACE A PAYMENTS FLOW BECOMES APPROVED. Called by the poster once
    SAP has returned a document key, which is the moment the final approval is
    genuinely complete.

    Idempotent, because the poster can be reached more than once for the same
    document (a recovery sweep, a reconciliation): a flow that is already
    APPROVED is left exactly as it is.
    """
    flow = flow_of(document)
    if flow is None or flow.status != FlowStatus.PENDING:
        return flow

    flow.status = FlowStatus.APPROVED
    _point_at(flow, None)
    flow.save(update_fields=['status', 'current_stage', 'current_user',
                             'updated_at'])
    return flow


@transaction.atomic
def return_to_final_stage(document, *, reason=''):
    """Leave a SAP-refused document with its final approver, ready to retry.

    Under the current lifecycle the flow never left that stage — the final
    approval does not complete it — so in the ordinary case this only refreshes
    the effective user and confirms the position. It stays because it is also
    the repair path: a flow that somehow completed without a posted document
    (a legacy row, a partially-applied recovery) is put back where it can be
    retried instead of being stranded as approved-but-unposted.

    NOTHING IS DELETED. The earlier APPROVE row stays; a REOPENED row is
    appended only when this actually moved the flow, so an ordinary SAP refusal
    does not litter the timeline with a reopening that never happened.
    """
    flow = flow_of(document)
    if flow is None or flow.status in (FlowStatus.REJECTED,
                                       FlowStatus.CANCELLED):
        return None

    stages = stages_for(flow)
    if not stages:
        return None

    last = stages[-1]
    moved = (flow.status != FlowStatus.PENDING
             or flow.current_stage_id != last.id)

    flow.status = FlowStatus.PENDING
    _point_at(flow, last.id)
    flow.save(update_fields=['status', 'current_stage', 'current_user',
                             'updated_at'])

    if moved:
        _log(document, action=PaymentStatusHistory.Action.REOPENED, user=None,
             stage_id=last.id,
             reason=reason or 'SAP refused the posting; returned for retry.',
             sequence=last.sequence, total=flow.total_stage)
    return flow


#: Kept as the previous name of `return_to_final_stage`, which is what the
#: poster and the recovery sweep called while the final approval still
#: completed the flow up front.
reopen_final_stage = return_to_final_stage


def notification_events_decision(document, *, approved, actor, reason=''):
    """Tell the submitter what was decided. Payments owns this (§9)."""
    from . import notification_events

    submitter = getattr(document, 'created_by', None)
    if isinstance(document, PaymentReceipt):
        notification_events.publish_receipt_decision(
            document, submitter, approved=approved, actor=actor, reason=reason)
    else:
        notification_events.publish_deposit_decision(
            document, submitter, approved=approved, actor=actor, reason=reason)



def stage_progress(document, flow):
    """Every stage of this document's workflow, and what happened at it.

    What the clients draw the approval ladder from:

        [{'sequence': 1, 'name': 'Stage 1', 'state': 'APPROVED',
          'approver': 'navdeep', 'approver_name': 'Navdeep Singh',
          'decided_by': 'navdeep', 'decided_at': ...}, ...]

    WHO APPROVED IS READ FROM HISTORY, NOT INFERRED FROM POSITION. The flow
    knows only where the document is now; the append-only history knows who
    actually cleared each stage and when, and it keeps knowing after the flow
    has moved on, been rejected, or completed. Inferring "stages below the
    current one must have been approved" would invent an approver for a stage
    that was skipped because it was deactivated mid-approval.

    WHO IS PENDING is the opposite: resolved LIVE from the engine
    (`effective_user_id`), because a reassignment or a replacement changes who
    must act on a document already waiting, and a stored name would be stale.

    Returns [] when the document has never been submitted.
    """
    if flow is None:
        return []

    stages = stages_for(flow)
    if not stages:
        return []

    from django.contrib.contenttypes.models import ContentType

    decided = {}
    rows = (PaymentStatusHistory.objects
            .filter(content_type=ContentType.objects.get_for_model(
                type(document)),
                object_id=document.pk,
                action=PaymentStatusHistory.Action.APPROVED)
            .order_by('created_at', 'id'))
    for row in rows:
        if row.stage_id:
            # LAST decision per stage wins: a retry at the final stage appends
            # a second APPROVED row, and the latest is the one that stands.
            decided[row.stage_id] = row

    # One query for every name, rather than one per stage.
    from django.contrib.auth import get_user_model

    wanted = {stage.effective_user_id for stage in stages
              if stage.effective_user_id}
    names = dict(get_user_model().objects
                 .filter(pk__in=wanted)
                 .values_list('pk', 'name')) if wanted else {}
    usernames = dict(get_user_model().objects
                     .filter(pk__in=wanted)
                     .values_list('pk', 'username')) if wanted else {}

    progress = []
    for stage in stages:
        row = decided.get(stage.id)
        if row is not None:
            state = 'APPROVED'
        elif stage.id == flow.current_stage_id and flow.status == FlowStatus.PENDING:
            state = 'CURRENT'
        else:
            state = 'PENDING'
        progress.append({
            'sequence': stage.sequence,
            'name': stage.name,
            'state': state,
            # Who must act here now, resolved live (replacements included).
            'approver': usernames.get(stage.effective_user_id, ''),
            'approver_name': names.get(stage.effective_user_id, ''),
            # Who actually decided it, from history. Empty until they do.
            'decided_by': (row.changed_by_username or '') if row else '',
            'decided_at': row.created_at if row else None,
        })
    return progress

# ---------------------------------------------------------------------------
# Queues
# ---------------------------------------------------------------------------

def _stage_ids_for(user, *, on_date=None):
    """Every stage this user may act at today, including as a stand-in."""
    from workflow.models import WorkflowStage
    from workflow.services import replacements

    acting_for = replacements.configured_users_acting_for(user.pk,
                                                          on_date=on_date)
    user_ids = {user.pk, *acting_for}
    return set(WorkflowStage.objects
               .filter(user_id__in=user_ids, is_active=True)
               .values_list('id', flat=True))


def pending_flows_for(user, model, *, on_date=None):
    """The documents waiting for THIS user, resolved from configuration.

    Never filtered on `current_user`: that column is a denormalised copy, and
    the whole point of holding a stage id is that the answer follows the
    configuration without anything here being rewritten.
    """
    stage_ids = _stage_ids_for(user, on_date=on_date)
    if not stage_ids:
        return model.objects.none()
    return (model.objects
            .filter(status=FlowStatus.PENDING, current_stage_id__in=stage_ids)
            .select_related('workflow'))


def pending_receipt_ids(user):
    return set(pending_flows_for(user, PaymentReceiptFlow)
               .values_list('receipt_id', flat=True))


def pending_deposit_ids(user):
    return set(pending_flows_for(user, BankDepositFlow)
               .values_list('deposit_id', flat=True))


def document_ids_for_view(user, view, model):
    """Approver-relative document ids for the list screens.

    `status` alone cannot express these: whether a PENDING_APPROVAL document is
    waiting on THIS user depends on which stage it stopped at and who that
    stage resolves to today.

    Returns None for an unknown view name, which the caller reads as "do not
    filter" — the same contract the old engine's helper had.
    """
    from django.contrib.contenttypes.models import ContentType

    from .models import PaymentReceipt, PaymentStatusHistory

    if view == 'mine':
        return set(model.objects.filter(created_by=user)
                   .values_list('id', flat=True))

    if view == 'awaiting_me':
        return (pending_receipt_ids(user) if model is PaymentReceipt
                else pending_deposit_ids(user))

    if view in ('approved_by_me', 'rejected_by_me'):
        action = (PaymentStatusHistory.Action.APPROVED
                  if view == 'approved_by_me'
                  else PaymentStatusHistory.Action.REJECTED)
        # Read from this module's own append-only history, which records who
        # acted — the decision is a fact about the past and never moves.
        username = getattr(user, 'username', '') or ''
        if not username:
            return set()
        return set(PaymentStatusHistory.objects.filter(
            content_type=ContentType.objects.get_for_model(model),
            action=action,
            changed_by_username=username,
        ).values_list('object_id', flat=True))

    return None
