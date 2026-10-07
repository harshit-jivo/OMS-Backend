"""The credit-limit business flow: submit, approve, reject.

Mirrors `backdate.services.flow` — read that module's docstring for the
engine/module split and why there is no task table. The differences:

* the customer facts are read from SAP at submission, not taken from the
  client (`services.sap.customer`);
* the final approval writes `CreditLimit` to the business partner;
* there is no edit path. Rejection is final, by decision, and an approved
  request is already in SAP. A wrong request is rejected and raised again.
"""
import logging

from django.db import transaction

from workflow.exceptions import WorkflowError
from workflow.services import replacements, selection
from workflow.services.assignments import get_stage_assignment

from credit_limit.models import (
    CreditLimitActionLog,
    CreditLimitAttachment,
    CreditLimitFlow,
    CreditLimitRequest,
    FlowStatus,
    LogAction,
)
from credit_limit.services import notify, sap

logger = logging.getLogger(__name__)

MODULE_CODE = 'CREDIT_LIMIT'


class CreditLimitError(Exception):
    """A business rule refused the operation. Carries a safe message."""


def log(request, *, action, user=None, stage_id=None, remarks=''):
    return CreditLimitActionLog.objects.create(
        request=request, action=action, acted_by=user, stage_id=stage_id,
        remarks=remarks or '')


def effective_user_id(stage_id):
    """Who must act on `stage_id` today, replacements applied."""
    if not stage_id:
        return None
    assignment = get_stage_assignment(stage_id)
    return assignment.effective_user_id if assignment else None


def _point_at(flow, stage_id):
    flow.current_stage_id = stage_id
    flow.current_user_id = effective_user_id(stage_id)


#: A batch is reviewed as one screen by its requester; beyond this it is a
#: bulk import, which this endpoint is not.
MAX_LINES = 50


class BatchError(CreditLimitError):
    """One or more lines of a submission failed; nothing was saved.

    `lines` is `[{index, card_code, message}]`, so the form can mark each
    failing row rather than showing the first error alone.
    """

    def __init__(self, lines):
        super().__init__(
            'Nothing was submitted: '
            + ('one party' if len(lines) == 1 else f'{len(lines)} parties')
            + ' could not be raised. Fix or remove '
            + ('it' if len(lines) == 1 else 'them') + ' and submit again.')
        self.lines = lines


#: Supporting documents per submission.
MAX_ATTACHMENTS = 10


def attachment_required(line_count):
    """THE rule: a single-party request needs at least one supporting
    document; a multi-party submission may go without. One definition, used by
    the API serializer and the invoice-review entry point alike."""
    return line_count == 1


def _create_one(*, user, company, card_code, new_credit_limit, valid_till,
                remarks, invoice_log):
    """Read the customer from SAP, store one request and route it."""
    try:
        card = sap.customer(company, card_code)
    except sap.SapUnavailable as exc:
        raise CreditLimitError(str(exc)) from exc
    if card is None:
        raise CreditLimitError(
            f'SAP has no customer {card_code} in {company}.')
    if card['card_type'] != 'C':
        raise CreditLimitError(f'{card_code} is not a customer in SAP.')

    request = CreditLimitRequest.objects.create(
        company=company,
        card_code=card['card_code'],
        card_name=card['card_name'],
        main_group=card['main_group'],
        current_balance=card['balance'],
        current_credit_limit=card['credit_limit'],
        new_credit_limit=new_credit_limit,
        valid_till=valid_till,
        remarks=remarks or '',
        invoice_log=invoice_log,
        created_by=user,
    )

    try:
        chosen = selection.select_for_module(
            module_code=MODULE_CODE, document_id=request.pk, company=company)
    except WorkflowError as exc:
        raise CreditLimitError(str(exc)) from exc

    flow = CreditLimitFlow(request=request, workflow=chosen.workflow,
                           total_stage=len(chosen.stages))
    _point_at(flow, chosen.stages[0].id)
    flow.save()
    log(request, action=LogAction.CREATE, user=user, remarks=remarks)
    return request, flow


@transaction.atomic
def submit(*, user, company, lines, remarks='', attachments=(),
           invoice_log=None):
    """Raise one request per line — each party is approved on its own chain.

    `lines` is `[{card_code, new_credit_limit, valid_till}]`. ALL OR NOTHING:
    every line is attempted (each in a savepoint) so all failures are
    reported together, and any failure rolls the whole submission back. A
    request no workflow matches is never stored — JSAP kept 30 such rows.

    The supporting documents are shared by every request in the submission:
    each file is written once, only after every line has routed (so a refused
    submission leaves nothing on disk), and linked from every request.
    """
    if not lines:
        raise CreditLimitError('Add at least one party.')
    if len(lines) > MAX_LINES:
        raise CreditLimitError(
            f'At most {MAX_LINES} parties can be raised in one submission.')
    attachments = [f for f in attachments if f]
    if attachment_required(len(lines)) and not attachments:
        raise CreditLimitError(
            'A supporting document is required for a single-party request.')
    if len(attachments) > MAX_ATTACHMENTS:
        raise CreditLimitError(
            f'At most {MAX_ATTACHMENTS} supporting documents per submission.')
    if invoice_log is not None and len(lines) != 1:
        raise CreditLimitError(
            "A request raised from an invoice covers that invoice's party only.")

    codes = [str(line['card_code']).strip().upper() for line in lines]
    duplicates = sorted({c for c in codes if codes.count(c) > 1})
    if duplicates:
        raise CreditLimitError(
            'Each party can appear once per submission: '
            + ', '.join(duplicates) + '.')

    created, failures = [], []
    for index, line in enumerate(lines):
        try:
            with transaction.atomic():
                created.append(_create_one(
                    user=user, company=company,
                    card_code=line['card_code'],
                    new_credit_limit=line['new_credit_limit'],
                    valid_till=line['valid_till'],
                    remarks=remarks, invoice_log=invoice_log))
        except CreditLimitError as exc:
            failures.append({'index': index, 'card_code': line['card_code'],
                             'message': str(exc)})
    if failures:
        raise BatchError(failures)

    requests = [req for req, _ in created]
    for upload in attachments:
        stored = CreditLimitAttachment.objects.create(
            request=requests[0], file=upload, name=upload.name[:255],
            uploaded_by=user)
        CreditLimitAttachment.objects.bulk_create(
            CreditLimitAttachment(request=req, file=stored.file.name,
                                  name=stored.name, uploaded_by=user)
            for req in requests[1:])

    # Notifications are recorded in this transaction and pushed on commit,
    # so a rolled-back submission tells nobody anything.
    notify.submitted([flow for _req, flow in created])
    return [req for req, _ in created]


def stages_for(flow):
    return selection.stages_for(flow.workflow)


def _locked(flow):
    return (CreditLimitFlow.objects.select_for_update()
            .select_related('request').get(pk=flow.pk))


def _guard(flow):
    if flow.status != FlowStatus.PENDING:
        raise CreditLimitError(
            f'This request is already {flow.get_status_display().lower()}.')
    if not flow.current_stage_id:
        raise CreditLimitError('This request is not waiting at any stage.')


@transaction.atomic
def approve(flow, *, user, remarks=''):
    """Approve the current stage; advance, or write SAP and complete.

    On the last stage SAP is written FIRST and `sap.SapWriteError` rolls the
    whole approval back — "approved" means the limit is in SAP.
    """
    flow = _locked(flow)
    _guard(flow)

    decided = flow.current_stage_id
    stages = stages_for(flow)
    current = next((s for s in stages if s.id == decided), None)
    following = ([s for s in stages if s.sequence > current.sequence]
                 if current else [])

    log(flow.request, action=LogAction.APPROVE, user=user, stage_id=decided,
        remarks=remarks)

    if following:
        _point_at(flow, following[0].id)
        flow.save(update_fields=['current_stage', 'current_user',
                                 'updated_at'])
        notify.stage_awaiting(flow)
        return flow

    sap.apply_limit(flow)
    flow.status = FlowStatus.APPROVED
    _point_at(flow, None)
    flow.save(update_fields=['status', 'current_stage', 'current_user',
                             'updated_at'])
    notify.decided(flow.request, approved=True, actor=user)
    return flow


@transaction.atomic
def reject(flow, *, user, remarks):
    """One rejection ends the request. Final."""
    if not (remarks or '').strip():
        raise CreditLimitError('A reason is required when rejecting a request.')

    flow = _locked(flow)
    _guard(flow)

    log(flow.request, action=LogAction.REJECT, user=user,
        stage_id=flow.current_stage_id, remarks=remarks)
    flow.status = FlowStatus.REJECTED
    _point_at(flow, None)
    flow.save(update_fields=['status', 'current_stage', 'current_user',
                             'updated_at'])
    notify.decided(flow.request, approved=False, actor=user, remarks=remarks)
    return flow


def pending_request_ids(user, *, on_date=None):
    """Requests awaiting THIS user today, stand-ins included.

    Resolved through the engine's replacements, never `current_user` — see
    `backdate.services.flow.pending_for`.
    """
    from workflow.models import WorkflowStage

    owners = replacements.configured_users_acting_for(
        user.pk, on_date or replacements.today())
    if not owners:
        return []
    stage_ids = WorkflowStage.objects.filter(
        user_id__in=owners, is_active=True).values_list('id', flat=True)
    return list(CreditLimitFlow.objects
                .filter(status=FlowStatus.PENDING,
                        current_stage_id__in=list(stage_ids))
                .values_list('request_id', flat=True))


def decided_request_ids(user, status=None):
    """Requests this user approved or rejected a stage of, optionally narrowed
    by the request's CURRENT status."""
    ids = (CreditLimitActionLog.objects
           .filter(acted_by=user,
                   action__in=[LogAction.APPROVE, LogAction.REJECT])
           .values_list('request_id', flat=True))
    qs = CreditLimitFlow.objects.filter(request_id__in=set(ids))
    if status:
        qs = qs.filter(status=status)
    return list(qs.values_list('request_id', flat=True))


def may_act_on(user, flow):
    """`(allowed, reason)`: holds the approval key AND is the current stage's
    effective user. Neither alone is enough — see `backdate.permissions`."""
    from credit_limit.permissions import has_approval_access

    if not has_approval_access(user):
        return False, 'You do not have permission to approve credit limits.'
    if not flow.current_stage_id:
        return False, 'This request is not awaiting approval.'
    effective = effective_user_id(flow.current_stage_id)
    if effective is None:
        return False, ('This stage is no longer configured. Ask a workflow '
                       'administrator to check the configuration.')
    if effective != user.pk:
        return False, 'This request is not awaiting your approval.'
    return True, ''


def progress(request):
    """Every stage of the request's workflow with its outcome so far."""
    flow = getattr(request, 'flow', None)
    if flow is None:
        return []
    decided = {entry.stage_id: entry
               for entry in request.action_logs.select_related('acted_by')
               if entry.action in (LogAction.APPROVE, LogAction.REJECT)
               and entry.stage_id}
    rows = []
    for stage in stages_for(flow):
        entry = decided.get(stage.id)
        assignment = get_stage_assignment(stage.id)
        if entry is not None:
            status = ('APPROVED' if entry.action == LogAction.APPROVE
                      else 'REJECTED')
        elif flow.current_stage_id == stage.id:
            status = 'AWAITING'
        elif flow.status == FlowStatus.PENDING:
            status = 'UPCOMING'
        else:
            status = 'SKIPPED'
        rows.append({
            'stage_id': stage.id,
            'sequence': stage.sequence,
            'stage_name': stage.name,
            'status': status,
            'reviewer': assignment.effective_username if assignment else '',
            'acted_by': (entry.acted_by.username
                         if entry and entry.acted_by else ''),
            'acted_at': entry.acted_at if entry else None,
            'remarks': entry.remarks if entry else '',
        })
    return rows
