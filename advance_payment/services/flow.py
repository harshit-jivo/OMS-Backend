"""The payment request's approval: who acts, and what each action does.

THE ROUTE
---------
The Workflow Engine chooses ONE workflow of module `ADVANCE_PAYMENT` for the
request (the workflow's queries read `advance_payment_request`: company,
department, sub-department). It may start with ANY number of approval
stages, named freely (none, or Sub-HOD, HOD, Director, ...), and must end
with three stages named exactly Payment Approval, Audit Approval, Final
Approval (see `StageRole`):

    approval stages (0 or more) approve, reject, or RETURN to the creator
    Payment                     fills the payment details; approves only when
                                they are complete and the payee is in SAP
    Audit                       approve or reject; nothing reaches SAP yet
    Final                       approving POSTS the outgoing payment to SAP
                                and, only once SAP has taken it, COMPLETES
                                the request: the money may go. Or SENDS BACK
                                to Payment, which rejects or corrects it, and
                                it goes through Audit and Final again

SAP IS WRITTEN ONCE, AT THE END. Nothing is posted before Final approves,
so a send-back, a correction or a rejection never has anything in SAP to
cancel or re-post, and a completed request is never changed again.

Rejection is terminal everywhere.

WHO MAY ACT: the current stage's user TODAY, replacements applied
(`get_stage_assignment`), checked on the server for every action. The
client never says who is acting.

THE CREATOR may edit or cancel while no stage has approved since they last
submitted, or while the request is RETURNED to them; after an edit of a
returned request they resubmit, and the engine chooses the route afresh.

Same shape as PRDO and BackDate: the flow row says where the request waits,
the log says what happened, the engine's configuration says what is ahead.
"""
import logging

from django.core import signing
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from workflow.exceptions import WorkflowError
from workflow.services import selection
from workflow.services.assignments import get_stage_assignment

from advance_payment.models import (
    FlowStatus,
    LogAction,
    FIXED_ROLES,
    Payout,
    PayoutLine,
    RETURN_TO_CREATOR_ROLES,
    RequestFlow,
    RequestLog,
    RequestStatus,
    StageRole,
)
from advance_payment.services import payout as payout_service
from advance_payment.services import requests as request_service
from advance_payment.services import sap as sap_service
from advance_payment.services import voucher as voucher_service

logger = logging.getLogger(__name__)

#: The code the module is registered under in the Workflows page.
MODULE_CODE = 'ADVANCE_PAYMENT'

#: Where each role may stand on a route: approvals first, then the three fixed stages.
ROLE_ORDER = [StageRole.APPROVAL, *FIXED_ROLES]


class FlowError(Exception):
    """An action was refused. `status` is the HTTP status the view answers with."""

    def __init__(self, message, *, status=400, problems=None):
        super().__init__(message)
        self.status = status
        self.problems = list(problems or [])


# ---------------------------------------------------------------------------
# The route, read from the engine
# ---------------------------------------------------------------------------

def roles(stages):
    """`[(stage, role)]` for a workflow's stages; FlowError if they break the rule.

    The rule: any number of approval stages, then exactly Payment Approval,
    Audit Approval, Final Approval, once each, last and in that order.
    """
    named = [(stage, StageRole.of(stage.name)) for stage in stages]
    fixed = [role for _s, role in named if role != StageRole.APPROVAL]
    tail = [role for _s, role in named[-len(FIXED_ROLES):]]
    if fixed != list(FIXED_ROLES) or tail != list(FIXED_ROLES):
        raise FlowError(
            'The workflow must end with three stages named exactly Payment Approval, '
            'Audit Approval and Final Approval, in that order. Any stages before them '
            'are approvals and may be named freely.', status=409)
    return named


def _stages(flow):
    return roles(selection.stages_for(flow.workflow))


def _select(advance):
    try:
        chosen = selection.select_for_module(
            module_code=MODULE_CODE, document_id=advance.pk, company=advance.company)
    except WorkflowError as exc:
        raise FlowError(str(exc), status=409) from exc
    named = roles(chosen.stages)
    return chosen, named


def effective_user_id(stage_id):
    if not stage_id:
        return None
    assignment = get_stage_assignment(stage_id)
    return assignment.effective_user_id if assignment else None


def _point_at(flow, stage, role):
    flow.current_stage_id = stage.id if stage else None
    flow.current_role = role or ''
    flow.current_user_id = effective_user_id(stage.id) if stage else None


def _save(flow):
    """Every move bumps `version`: a decision made on a stale screen is refused."""
    flow.version += 1
    flow.save()


# ---------------------------------------------------------------------------
# The log
# ---------------------------------------------------------------------------

#: `log(stage=...)` default: the stage the flow waits at.
_CURRENT = object()


def log(advance, action, *, user=None, flow=None, remarks='', data=None,
        from_status='', to_status='', stage=_CURRENT):
    """Append one row. The stage is snapshotted: renaming it later rewrites nothing.

    `stage=None` for a row that belongs to no stage (created, edited, cancelled).
    """
    flow = flow if flow is not None else RequestFlow.objects.filter(request=advance).first()
    if stage is _CURRENT:
        stage = flow.current_stage if flow and flow.current_stage_id else None
    on_behalf_of = None
    if stage is not None and user is not None and stage.user_id != user.pk:
        on_behalf_of = stage.user_id
    return RequestLog.objects.create(
        request=advance, action=action, cycle=flow.cycle if flow else 1,
        stage=stage, stage_name=stage.name if stage else '',
        stage_sequence=stage.sequence if stage else None,
        actor=user, on_behalf_of_id=on_behalf_of, from_status=from_status,
        to_status=to_status, remarks=remarks or '', data=data)


# ---------------------------------------------------------------------------
# Creating and resubmitting
# ---------------------------------------------------------------------------

@transaction.atomic
def raise_request(cleaned, *, user, files=()):
    """Create the request and route it. Atomic: a request that cannot be routed is not kept."""
    advance = request_service.create(cleaned, user=user, files=files)
    chosen, named = _select(advance)
    first, role = named[0]
    flow = RequestFlow(request=advance, workflow=chosen.workflow,
                       matched_query=chosen.matched_query, total_stages=len(named))
    _point_at(flow, first, role)
    flow.save()
    log(advance, LogAction.CREATED, user=user, flow=flow, stage=None,
        to_status=RequestStatus.IN_APPROVAL,
        data={'workflow': chosen.workflow.code, 'files': [f.name for f in advance.files.all()]})
    log(advance, LogAction.SUBMITTED, user=user, flow=flow, stage=None,
        data={'waiting_at': first.name})
    return advance


def _reroute(flow, advance):
    """Choose the route afresh and start at its first stage."""
    chosen, named = _select(advance)
    first, role = named[0]
    flow.workflow = chosen.workflow
    flow.matched_query = chosen.matched_query
    flow.total_stages = len(named)
    flow.status = FlowStatus.PENDING
    _point_at(flow, first, role)
    return first


def creator_may_change(advance, flow=None):
    """RETURNED to them, or pending with no approval since they last submitted."""
    flow = flow or RequestFlow.objects.filter(request=advance).first()
    if flow is None:
        return False
    if flow.status == FlowStatus.RETURNED:
        return True
    if flow.status != FlowStatus.PENDING:
        return False
    last_submit = (RequestLog.objects
                   .filter(request=advance, action__in=[LogAction.SUBMITTED, LogAction.RESUBMITTED])
                   .order_by('-id').values_list('id', flat=True).first()) or 0
    return not RequestLog.objects.filter(
        request=advance, action=LogAction.APPROVED, id__gt=last_submit).exists()


def _locked(advance):
    # `of=` is load-bearing, not a micro-optimisation. `current_stage` is
    # nullable, so select_related() renders it as a LEFT OUTER JOIN, and
    # Postgres rejects a bare FOR UPDATE that covers the nullable side of one:
    #   NotSupportedError: FOR UPDATE cannot be applied to the nullable side
    #                      of an outer join
    # That made every approve/reject return a 500. Naming the rows to lock
    # emits `FOR UPDATE OF flow, request`, which leaves the outer-joined
    # stage unlocked and satisfies Postgres.
    #
    # Both named rows are on the inner side: `request` is a non-nullable
    # OneToOneField. `current_stage` does not need locking - it is read to
    # decide the next step, never mutated here, and the flow row being locked
    # is what serialises two approvers acting at once.
    return (RequestFlow.objects
            .select_for_update(of=('self', 'request'))
            .select_related('request', 'current_stage')
            .get(request=advance))


def _check_version(flow, version):
    if version not in (None, '') and int(version) != flow.version:
        raise FlowError('This request changed since you opened it. Reload it and try again.',
                        status=409)


def _creator(advance, user):
    if advance.created_by_id != user.pk:
        raise FlowError('Only the person who raised this request may do that.', status=403)


@transaction.atomic
def edit(advance, cleaned, *, user, files=(), remove_file_ids=(), resubmit=False, version=None):
    """The creator's edit. A returned request may be resubmitted in the same step."""
    flow = _locked(advance)
    _creator(advance, user)
    _check_version(flow, version)
    if not creator_may_change(advance, flow):
        raise FlowError('It has been approved since you raised it, so it can no longer be edited.',
                        status=409)
    changes = request_service.update(flow.request, cleaned, user=user, files=files,
                                     remove_file_ids=remove_file_ids)
    advance = flow.request
    if changes:
        log(advance, LogAction.EDITED, user=user, flow=flow, stage=None, data=changes)
    if flow.status == FlowStatus.PENDING and changes:
        # Nothing is approved yet, so nothing is lost by routing again: a new
        # department must reach its own approvers.
        _reroute(flow, advance)
        _save(flow)
    elif flow.status == FlowStatus.RETURNED and resubmit:
        _resubmit(flow, advance, user=user)
    else:
        _save(flow)
    return advance


def _resubmit(flow, advance, *, user, remarks=''):
    flow.cycle += 1
    first = _reroute(flow, advance)
    _save(flow)
    advance.status = RequestStatus.IN_APPROVAL
    advance.save(update_fields=['status', 'updated_on'])
    log(advance, LogAction.RESUBMITTED, user=user, flow=flow, stage=None, remarks=remarks,
        from_status=RequestStatus.RETURNED, to_status=RequestStatus.IN_APPROVAL,
        data={'workflow': flow.workflow.code, 'waiting_at': first.name})


@transaction.atomic
def resubmit(advance, *, user, remarks='', version=None):
    flow = _locked(advance)
    _creator(advance, user)
    _check_version(flow, version)
    if flow.status != FlowStatus.RETURNED:
        raise FlowError('Only a request returned to you can be resubmitted.', status=409)
    _resubmit(flow, flow.request, user=user, remarks=remarks)
    return flow.request


@transaction.atomic
def cancel(advance, *, user, remarks='', version=None):
    flow = _locked(advance)
    _creator(advance, user)
    _check_version(flow, version)
    if not creator_may_change(advance, flow):
        raise FlowError('It has been approved since you raised it, so it can no longer be '
                        'cancelled. Ask an approver to reject it.', status=409)
    advance = flow.request
    before = advance.status
    flow.status = FlowStatus.CANCELLED
    _point_at(flow, None, '')
    _save(flow)
    advance.status = RequestStatus.CANCELLED
    advance.cancelled_by = user
    advance.cancelled_on = timezone.now()
    advance.save(update_fields=['status', 'cancelled_by', 'cancelled_on', 'updated_on'])
    log(advance, LogAction.CANCELLED, user=user, flow=flow, stage=None, remarks=remarks,
        from_status=before, to_status=RequestStatus.CANCELLED)
    return advance


# ---------------------------------------------------------------------------
# The stages' actions
# ---------------------------------------------------------------------------

def is_actor(flow, user):
    return (flow is not None and flow.status == FlowStatus.PENDING and flow.current_stage_id
            and effective_user_id(flow.current_stage_id) == user.pk)


def _acting(advance, user, version):
    """The locked flow, if `user` is its current stage's user today."""
    flow = _locked(advance)
    _check_version(flow, version)
    if flow.status != FlowStatus.PENDING or not flow.current_stage_id:
        raise FlowError(f'This request is {flow.request.get_status_display().lower()}; '
                        f'it is not waiting for a decision.', status=409)
    if effective_user_id(flow.current_stage_id) != user.pk:
        raise FlowError(f'It is waiting at {flow.current_stage.name}, which is not yours.',
                        status=403)
    return flow


def _remarks_required(remarks, what):
    if not (remarks or '').strip():
        raise FlowError(f'Say why, in the remarks, when {what}.')


def _next(flow):
    """The stage after the current one, as configured now; None after the last."""
    named = _stages(flow)
    current = next((s for s, _r in named if s.id == flow.current_stage_id), None)
    if current is not None:
        return next(((s, r) for s, r in named if s.sequence > current.sequence), None)
    # Its stage was deactivated mid-flight: carry on from the first active
    # stage after where it stood, so the request is not stranded.
    if flow.current_stage is not None:
        return next(((s, r) for s, r in named if s.sequence > flow.current_stage.sequence), None)
    index = ROLE_ORDER.index(StageRole(flow.current_role))
    return next(((s, r) for s, r in named if ROLE_ORDER.index(r) > index), None)


def _link_partner(flow, user):
    """A not-in-SAP employee: find the advance account created for them since."""
    advance = flow.request
    code = advance.partner_code[len(request_service.NOT_IN_SAP_PREFIX):]
    try:
        account = sap_service.employee_advance_account(advance.company, code)
    except sap_service.SapUnavailable as exc:
        raise FlowError(f'Could not check SAP for {code}: {exc}', status=503) from exc
    if account is None:
        raise FlowError(f'{advance.partner_name} ({code}) has no advance account in SAP yet. '
                        f'Create their employee advance account in SAP, then approve.', status=409)
    old = advance.partner_code
    advance.partner_code = account['acct_code']
    advance.partner_not_in_sap = False
    advance.save(update_fields=['partner_code', 'partner_not_in_sap', 'updated_on'])
    log(advance, LogAction.PARTNER_LINKED, user=user, flow=flow,
        data={'old': old, 'new': account['acct_code'], 'account': account['acct_name']})


def _post_voucher(flow, user):
    """Final: post the outgoing payment to SAP. Returns SAP's refusal, or ''.

    Posted once. A retry after a failure (or after an answer SAP never sent)
    goes through `voucher.post`, which finds a payment SAP already took by
    its journal memo instead of posting a second one.
    """
    advance = flow.request
    if voucher_service.live(advance) is not None:
        return ''
    outcome = voucher_service.post(advance, user=user)
    if not outcome.ok:
        log(advance, LogAction.SAP_POST_FAILED, user=user, flow=flow,
            data={'error': outcome.message, 'voucher': outcome.voucher.version})
        return outcome.message
    log(advance, LogAction.SAP_POSTED, user=user, flow=flow,
        data={'doc_num': outcome.voucher.sap_doc_num, 'doc_entry': outcome.voucher.sap_doc_entry,
              'voucher': outcome.voucher.version})
    return ''


def approve(advance, *, user, remarks='', version=None):
    """Approve the current stage. Returns `(advance, error)`.

    `error` is SAP's refusal at the Final stage: the attempt is recorded (the
    voucher row and the log) and the request stays at Final, so this does
    not roll back, and the view reports it. The request is COMPLETED only
    once SAP has the payment.
    """
    with transaction.atomic():
        flow = _acting(advance, user, version)
        advance = flow.request
        role = StageRole(flow.current_role)

        if role == StageRole.PAYMENT:
            if advance.partner_not_in_sap:
                _link_partner(flow, user)
            problems = payout_service.problems(advance)
            if problems:
                raise FlowError('The payment details are not ready: ' + ' '.join(problems),
                                problems=problems)
        if role == StageRole.FINAL:
            error = _post_voucher(flow, user)
            if error:
                _save(flow)
                return advance, error

        log(advance, LogAction.APPROVED, user=user, flow=flow, remarks=remarks)
        following = _next(flow)
        if role == StageRole.FINAL or following is None:
            flow.status = FlowStatus.COMPLETED
            _point_at(flow, None, '')
            _save(flow)
            advance.status = RequestStatus.COMPLETED
            advance.save(update_fields=['status', 'updated_on'])
            log(advance, LogAction.COMPLETED, user=user, flow=flow, stage=None,
                from_status=RequestStatus.IN_APPROVAL, to_status=RequestStatus.COMPLETED)
        else:
            _point_at(flow, *following)
            _save(flow)
        return advance, ''


@transaction.atomic
def reject(advance, *, user, remarks, version=None):
    """Reject: terminal. Nothing is in SAP before Final, so there is nothing to undo."""
    _remarks_required(remarks, 'rejecting')
    flow = _acting(advance, user, version)
    advance = flow.request
    log(advance, LogAction.REJECTED, user=user, flow=flow, remarks=remarks,
        from_status=advance.status, to_status=RequestStatus.REJECTED)
    flow.status = FlowStatus.REJECTED
    _point_at(flow, None, '')
    _save(flow)
    advance.status = RequestStatus.REJECTED
    advance.save(update_fields=['status', 'updated_on'])
    return advance


@transaction.atomic
def return_to_creator(advance, *, user, remarks, version=None):
    """Sub-HOD, HOD or Director: back to the creator, to edit and resubmit."""
    _remarks_required(remarks, 'returning it')
    flow = _acting(advance, user, version)
    if StageRole(flow.current_role) not in RETURN_TO_CREATOR_ROLES:
        raise FlowError('Only the approval stages before Payment may return a request to its creator.',
                        status=409)
    advance = flow.request
    log(advance, LogAction.RETURNED, user=user, flow=flow, remarks=remarks,
        from_status=RequestStatus.IN_APPROVAL, to_status=RequestStatus.RETURNED)
    flow.status = FlowStatus.RETURNED
    _point_at(flow, None, '')
    _save(flow)
    advance.status = RequestStatus.RETURNED
    advance.save(update_fields=['status', 'updated_on'])
    return advance


#: Stages that may hand a request back to Payment for correction. Audit is
#: here as well as Final because Audit is the first desk that reads the
#: payment details against the evidence: making it approve something it can
#: see is wrong, purely so Final can be the one to send it back, adds a
#: pointless round trip and an approval nobody meant.
SEND_BACK_ROLES = (StageRole.FINAL, StageRole.AUDIT)


@transaction.atomic
def send_back(advance, *, user, remarks, version=None):
    """Audit or Final: back to Payment, which rejects or corrects it.

    The request then climbs the same stages again (Payment -> Audit -> Final),
    with `cycle` incremented so the creator's edit rule and the "approved in
    this round?" checks treat it as a fresh round.
    """
    _remarks_required(remarks, 'sending it back')
    flow = _acting(advance, user, version)
    if StageRole(flow.current_role) not in SEND_BACK_ROLES:
        raise FlowError('Only the Audit or Final stage may send a request back '
                        'to Payment.', status=409)
    payment = next(((s, r) for s, r in _stages(flow) if r == StageRole.PAYMENT), None)
    advance = flow.request
    log(advance, LogAction.SENT_BACK, user=user, flow=flow, remarks=remarks,
        data={'to': payment[0].name})
    flow.cycle += 1
    _point_at(flow, *payment)
    _save(flow)
    return advance


# ---------------------------------------------------------------------------
# Payment details, files and UTRs
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Typing the payee's bank account by hand: password first
# ---------------------------------------------------------------------------
#
# The payee's account is normally PICKED from the accounts SAP holds for
# them. Typing one by hand (a vendor with none, or a new one; every Employee,
# whose advance account is a G/L with no bank details) is where money goes to
# a wrong account, so the Payment user confirms their password first:
#
#   POST /requests/<id>/confirm-password/  ->  a token, good for this request,
#                                              this user, MANUAL_TOKEN_SECONDS
#   PUT  /requests/<id>/payout/  {..., manual_token}
#
# The server decides what is "by hand": an account (and IFSC) that is not one
# of the payee's SAP accounts. The client's `to_account_manual` is not
# trusted for it. An unchanged account saved again needs no new token.

MANUAL_TOKEN_SALT = 'advance-payment.manual-account'
#: How long one password confirmation unlocks typing, in seconds.
MANUAL_TOKEN_SECONDS = 30 * 60
#: Wrong passwords allowed per user in MANUAL_LOCK_SECONDS before refusing.
MANUAL_MAX_FAILURES = 5
MANUAL_LOCK_SECONDS = 15 * 60


def _failures_key(user):
    return f'advance-payment:manual-pw-failures:{user.pk}'


def confirm_password(advance, *, user, password):
    """The Payment user's password, for typing an account. Returns a token."""
    flow = RequestFlow.objects.select_related('current_stage').get(request=advance)
    if not is_actor(flow, user) or flow.current_role != StageRole.PAYMENT:
        raise FlowError('Only the Payment stage user may enter a bank account by hand.', status=403)
    failures = cache.get(_failures_key(user), 0)
    if failures >= MANUAL_MAX_FAILURES:
        raise FlowError('Too many wrong passwords. Try again in 15 minutes.', status=429)
    if not password or not user.check_password(password):
        cache.set(_failures_key(user), failures + 1, MANUAL_LOCK_SECONDS)
        logger.warning('ADVANCE: wrong password confirming a manual account on %s by user %s',
                       advance.pk, user.pk)
        raise FlowError('That password is not right.', status=403)
    cache.delete(_failures_key(user))
    return signing.dumps({'r': advance.pk, 'u': user.pk}, salt=MANUAL_TOKEN_SALT)


def _token_ok(token, advance, user):
    try:
        data = signing.loads(token or '', salt=MANUAL_TOKEN_SALT, max_age=MANUAL_TOKEN_SECONDS)
    except signing.BadSignature:
        return False
    return data.get('r') == advance.pk and data.get('u') == user.pk


def _digits(value):
    return ''.join(ch for ch in str(value or '') if ch.isdigit())


def _from_sap(advance, account, ifsc):
    """Is (account, IFSC) one of the payee's own SAP accounts?

    An Employee's payee is a G/L with no bank details: never. SAP out of
    reach: treated as typed, so the password is asked rather than skipped.
    """
    if advance.request_type == 'EMPLOYEE_ADVANCE':
        return False
    try:
        rows, _default = sap_service.partner_bank_accounts(advance.company, advance.partner_code)
    except (sap_service.SapUnavailable, sap_service.UnknownCompany):
        return False
    for row in rows:
        if _digits(row.get('account_number')) == account:
            sap_ifsc = (row.get('ifsc') or '').upper()
            return not sap_ifsc or sap_ifsc == ifsc
    return False


@transaction.atomic
def save_payout(advance, data, *, user, version=None):
    """Payment stage only. Nothing is in SAP yet, so a change is only a change here.

    A payee account typed by hand needs `manual_token` (see above).
    """
    flow = _acting(advance, user, version)
    if StageRole(flow.current_role) != StageRole.PAYMENT:
        raise FlowError('The payment details are filled in at the Payment stage.', status=409)
    advance = flow.request
    data = dict(data or {})
    account = _digits(data.get('to_account_number'))
    ifsc = str(data.get('to_ifsc') or '').strip().upper()
    stored = Payout.objects.filter(request=advance).first()
    unchanged = (stored is not None and _digits(stored.to_account_number) == account
                 and (stored.to_ifsc or '').upper() == ifsc)
    manual = bool(account) and not _from_sap(advance, account, ifsc)
    if manual and not unchanged and not _token_ok(data.get('manual_token'), advance, user):
        raise FlowError('Confirm your password to enter the bank account by hand.', status=403,
                        problems=['manual_password'])
    data['to_account_manual'] = manual if account else bool(data.get('to_account_manual'))
    try:
        _payout, changed = payout_service.save(advance, data, user=user)
    except payout_service.PayoutInvalid as exc:
        raise FlowError(str(exc), problems=exc.problems) from exc
    if changed:
        log(advance, LogAction.PAYOUT_UPDATED, user=user, flow=flow,
            data={'manual_account': manual, 'account_last4': account[-4:] if account else ''}
            if manual and not unchanged else None)
    _save(flow)
    return advance


def payment_users(flow):
    """Today's users of the route's Payment and Final stages (UTRs, after completion)."""
    ids = set()
    for stage, role in _stages(flow):
        if role in (StageRole.PAYMENT, StageRole.FINAL):
            ids.add(effective_user_id(stage.id))
    return ids


@transaction.atomic
def record_utr(advance, line_id, *, user, utr, proof=None):
    flow = _locked(advance)
    if flow.status != FlowStatus.COMPLETED:
        raise FlowError('A UTR is recorded once the request is completed and paid.', status=409)
    if user.pk not in payment_users(flow):
        raise FlowError('Only the Payment or Final approver records the UTR.', status=403)
    line = PayoutLine.objects.filter(pk=line_id, payout__request=advance).first()
    if line is None:
        raise FlowError('No such payment line.', status=404)
    try:
        old = payout_service.record_utr(line, utr=utr, proof=proof, user=user)
    except payout_service.PayoutInvalid as exc:
        raise FlowError(str(exc)) from exc
    log(flow.request, LogAction.UTR_RECORDED, user=user, flow=flow, stage=None,
        data={'line': line.pk, 'method': line.method, 'amount': str(line.amount),
              'utr': line.utr, 'old': old or None,
              'read_from': (proof or {}).get('fileName') if isinstance(proof, dict) else None})
    return flow.request


def may_add_file(flow, user, purpose):
    """Payment proofs and bank proofs: the Payment stage, or the UTR recorders after."""
    if flow is None:
        return False
    if is_actor(flow, user) and flow.current_role == StageRole.PAYMENT:
        return True
    return flow.status == FlowStatus.COMPLETED and user.pk in payment_users(flow)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def abilities(advance, user):
    """What `user` may do to it now: the buttons the pages show."""
    flow = getattr(advance, 'flow', None)
    mine = advance.created_by_id == user.pk
    actor = bool(is_actor(flow, user))
    role = flow.current_role if actor else ''
    may_change = mine and creator_may_change(advance, flow)
    completed = flow is not None and flow.status == FlowStatus.COMPLETED
    try:
        utr = completed and user.pk in payment_users(flow)
    except FlowError:
        utr = False
    return {
        'edit': may_change,
        'cancel': may_change,
        'resubmit': mine and flow is not None and flow.status == FlowStatus.RETURNED,
        'approve': actor,
        'reject': actor,
        'return_to_creator': actor and role in RETURN_TO_CREATOR_ROLES,
        'send_back': actor and role in SEND_BACK_ROLES,
        'edit_payout': actor and role == StageRole.PAYMENT,
        'record_utr': bool(utr),
    }


def desk_request_ids(user):
    """What the approval desk LISTS for `user`: only what is theirs to act on.

    * waiting at a stage that is theirs today (replacements applied);
    * completed, on a route where they hold Payment or Final: the UTR is
      theirs to record after paying.

    Not every request on their workflows, and not the ones they have passed
    on: once approved, a request leaves their list (it stays readable, see
    `readable_request_ids`).
    """
    from workflow.models import WorkflowStage
    from workflow.services import replacements

    owners = replacements.configured_users_acting_for(user.pk, replacements.today())
    payers = WorkflowStage.objects.filter(
        user_id__in=owners, is_active=True, workflow__module__code=MODULE_CODE,
        name__iregex=r'^\s*(payment|final)\s+approval\s*$').values_list('workflow_id', flat=True)
    ids = awaiting_ids(user)
    ids |= set(RequestFlow.objects.filter(status=FlowStatus.COMPLETED, workflow_id__in=list(payers))
               .values_list('request_id', flat=True))
    return ids


def readable_request_ids(user):
    """What `user` may OPEN from the desk: their list, and what they have acted on."""
    ids = desk_request_ids(user)
    ids |= set(RequestLog.objects.filter(actor=user).values_list('request_id', flat=True))
    return ids


def awaiting_ids(user):
    """Requests whose current stage is `user`'s today."""
    from workflow.models import WorkflowStage
    from workflow.services import replacements

    owners = replacements.configured_users_acting_for(user.pk, replacements.today())
    stage_ids = WorkflowStage.objects.filter(user_id__in=owners, is_active=True).values_list('id', flat=True)
    return set(RequestFlow.objects.filter(status=FlowStatus.PENDING, current_stage_id__in=list(stage_ids))
               .values_list('request_id', flat=True))


def stage_plan(advance):
    """Every stage of the route with where the request stands on it, for the timeline."""
    flow = getattr(advance, 'flow', None)
    if flow is None:
        return []
    try:
        named = _stages(flow)
    except FlowError:
        named = [(s, StageRole.of(s.name)) for s in selection.stages_for(flow.workflow)]
    decided = {}
    for row in advance.logs.filter(cycle=flow.cycle, stage__isnull=False,
                                   action__in=[LogAction.APPROVED, LogAction.REJECTED,
                                               LogAction.RETURNED, LogAction.SENT_BACK]):
        decided[row.stage_id] = row
    out = []
    for stage, role in named:
        row = decided.get(stage.id)
        if flow.current_stage_id == stage.id and flow.status == FlowStatus.PENDING:
            state = 'CURRENT'
        elif row is not None:
            state = row.action
        else:
            state = 'UPCOMING'
        assignment = get_stage_assignment(stage.id)
        out.append({
            'stage_id': stage.id,
            'name': stage.name,
            'role': role or '',
            'sequence': stage.sequence,
            'state': state,
            'user_id': assignment.effective_user_id if assignment else None,
            'user_name': assignment.effective_username if assignment else '',
            'acted_by_id': row.actor_id if row else None,
            'acted_on': row.created_on.isoformat() if row else None,
        })
    return out
