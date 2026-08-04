"""All approval state transitions live here.

Deliberately modelled on tracker/services.py, whose docstring states the intent:
"All stage-movement rules live here so the views stay thin and the logic is
testable in isolation." Two things it does that orders/views.py does not, and
which this module must not lose:

  * @transaction.atomic on every transition
  * select_for_update() on the row being decided

`UpdateOrderStatusView.post` (orders/views.py:2895) is neither, across ~380
lines and several dependent writes — so two approvers acting at once can both
pass the pending check and double-advance a document.
"""
import logging

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone

from .models import (
    ApprovalAction,
    ApprovalLevel,
    ApprovalLevelApprover,
    ApprovalRequest,
    ApprovalWorkflow,
)

logger = logging.getLogger(__name__)

# Registered by each domain app in AppConfig.ready(). Keeps the engine from
# importing `payments` (which would be a circular dependency) while still
# letting an approval outcome drive domain state.
_HOOKS = {}


def register_hooks(model_label, *, on_approved=None, on_rejected=None,
                   on_submitted=None, on_cancelled=None):
    """Wire domain callbacks for a document type. Called from apps.py ready()."""
    _HOOKS[model_label.lower()] = {
        'approved': on_approved,
        'rejected': on_rejected,
        'submitted': on_submitted,
        'cancelled': on_cancelled,
    }


def _fire(event, request):
    """Run a domain hook INSIDE the approval transaction.

    Atomicity is the point: a payment cannot be 'approved' without also being
    queued for SAP, because both writes commit together or neither does.
    """
    label = request.content_type.model_class()._meta.label_lower
    hook = (_HOOKS.get(label) or {}).get(event)
    if hook:
        hook(request)


# ---------------------------------------------------------------------------
# Approver resolution
# ---------------------------------------------------------------------------

def eligible_approver_ids(level, company=''):
    """Users who may act at `level`.

    Named approvers NARROW the level; the role is only a fallback:

      * named approvers exist -> ONLY those users
      * no named approvers    -> anyone holding the level's role

    That ordering is what makes a multi-level ladder work. Two levels commonly
    share one role (both rungs are "payment_approver"), so unioning role holders
    with the named list made every approver eligible at every level — level 2's
    approver could act while level 1 was still open, and both saw the same
    document at once. Naming a user on a rung is an explicit statement that
    *this* person owns *this* rung, so it must win over the broader role.

    A level with neither a role nor grants has no eligible approver — the caller
    surfaces that at submit time rather than letting the document deadlock at
    that rung days later.
    """
    from users.models import User

    grants = ApprovalLevelApprover.objects.filter(level=level, is_active=True)
    if company:
        grants = grants.filter(Q(company=company) | Q(company=''))
    named = set(grants.values_list('user_id', flat=True))
    if named:
        return named

    if not level.role_id:
        return set()

    # Match the PRIMARY role or any extra role — a Manager granted "Payment
    # Approver" via extra_roles keeps "manager" as their primary, so filtering
    # on role_id alone would never find them.
    return set(
        User.objects.filter(
            Q(role_id=level.role_id) | Q(extra_roles__id=level.role_id),
            is_active=True,
        )
        .values_list('id', flat=True)
        .distinct()
    )


def can_act(user, request):
    """Whether `user` may decide `request` at its current level."""
    if not user or not user.is_authenticated or request.status != ApprovalRequest.Status.PENDING:
        return False
    if request.workflow.forbid_self_approval and request.submitted_by_id == user.id:
        return False
    if not has_approve_permission(user, request.workflow.document_type):
        return False
    level = _level_at(request, request.current_level)
    if level is None:
        return False
    return user.id in eligible_approver_ids(level, request.company)


def has_approve_permission(user, document_type):
    """Does `user` hold the module-level grant to approve this document type?

    Being named on a level is necessary but not sufficient: an admin must ALSO
    have granted Payments_Approve / Deposit_Approve. Checking it inside can_act
    means the API, the `can_act` flag the UI reads, and the approve/reject
    service calls all consult one rule rather than three.

    Document types with no dedicated grant (e.g. ORDER) are unaffected.
    """
    # Imported here rather than at module scope: payments imports approvals for
    # its hooks, so a top-level import would be circular.
    from payments.permissions import (
        DEPOSIT_APPROVE,
        PAYMENTS_APPROVE,
        has_permission_key,
    )

    required = {
        'PAYMENT': PAYMENTS_APPROVE,
        'DEPOSIT': DEPOSIT_APPROVE,
    }.get(str(document_type).upper())

    return True if required is None else has_permission_key(user, required)


def _level_at(request, position):
    """The active level at 1-based LADDER POSITION `position`.

    `current_level` counts rungs (1, 2, 3 ...), NOT the level's `sequence`
    column. Those two only coincide when the sequences happen to start at 1 and
    have no gaps — delete the first level of a workflow and they diverge, at
    which point matching on `sequence` finds nothing and every document
    deadlocks with "this level no longer exists". Ordering and indexing makes
    the engine independent of how the sequences are numbered.
    """
    levels = list(
        ApprovalLevel.objects
        .filter(workflow_id=request.workflow_id, is_active=True)
        .order_by('sequence')
    )
    if position < 1 or position > len(levels):
        return None
    return levels[position - 1]


def resolve_workflow(*, document_type, company):
    """The active workflow for a document type, preferring a company-specific
    one over the catch-all."""
    qs = ApprovalWorkflow.objects.filter(
        document_type=document_type, is_active=True
    ).filter(Q(company=company) | Q(company=''))
    return qs.order_by('-company').first()      # non-blank company sorts first


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _next_sequence(request):
    top = request.actions.aggregate(m=Max('sequence'))['m'] or 0
    return top + 1


def _log(request, *, action, user, remarks='', level=0, level_name='', ctx=None):
    ctx = ctx or {}
    role = getattr(getattr(user, 'role', None), 'name', '') if user else ''
    return ApprovalAction.objects.create(
        request=request,
        sequence=_next_sequence(request),
        round_number=request.round_number,
        level=level,
        level_name=level_name,
        action=action,
        remarks=remarks or '',
        approver=user,
        approver_username=getattr(user, 'username', '') or '',
        approver_role=role or '',
        ip_address=ctx.get('ip'),
        user_agent=(ctx.get('user_agent') or '')[:400],
    )


def _approvals_at_current_level(request):
    return (
        request.actions.filter(
            round_number=request.round_number,
            level=request.current_level,
            action=ApprovalAction.Action.APPROVE,
        )
        .values('approver_id')
        .distinct()
        .count()
    )


# ---------------------------------------------------------------------------
# Public transitions
# ---------------------------------------------------------------------------

@transaction.atomic
def submit(*, document, user, company, amount, document_number='',
           document_type, ctx=None):
    """Create (or reopen) the approval request for a document and start level 1.

    Raises ValidationError if no workflow is configured or a level has nobody
    who could ever approve it — failing at submit beats deadlocking later.
    """
    workflow = resolve_workflow(document_type=document_type, company=company)
    if workflow is None:
        raise ValidationError(
            f'No active approval workflow for {document_type} / {company}.')

    levels = list(workflow.levels.filter(is_active=True).order_by('sequence'))
    if not levels:
        raise ValidationError(f'Workflow {workflow.code} has no active levels.')

    for level in levels:
        if not eligible_approver_ids(level, company):
            raise ValidationError(
                f'No eligible approver for "{level.name}". '
                f'Assign a role or a user to that level first.')

    ct = ContentType.objects.get_for_model(document.__class__)
    now = timezone.now()

    existing = (
        ApprovalRequest.objects.select_for_update()
        .filter(content_type=ct, object_id=document.pk)
        .exclude(status=ApprovalRequest.Status.CANCELLED)
        .order_by('-created_at')
        .first()
    )

    if existing and existing.status == ApprovalRequest.Status.REJECTED:
        # Resubmit. Prior actions are NEVER deleted — round_number rises so the
        # history of the earlier attempt stays attributable. This is exactly the
        # reset that UpdateOrderView.put omits (orders/views.py:2208-2347),
        # which leaves stale REJECTED rows and deadlocks the order permanently.
        existing.round_number += 1
        existing.status = ApprovalRequest.Status.PENDING
        existing.current_level = 1
        existing.total_levels = len(levels)
        existing.amount = amount
        existing.submitted_by = user
        existing.submitted_at = now
        existing.level_entered_at = now
        existing.decided_at = None
        existing.save()
        _log(existing, action=ApprovalAction.Action.RESUBMIT, user=user,
             level=1, level_name=levels[0].name, ctx=ctx)
        _fire('submitted', existing)
        return existing

    if existing and existing.status in (
            ApprovalRequest.Status.PENDING, ApprovalRequest.Status.APPROVED):
        raise ValidationError('This document already has an open approval request.')

    request = ApprovalRequest.objects.create(
        workflow=workflow,
        content_type=ct,
        object_id=document.pk,
        company=company,
        amount=amount,
        document_number=document_number,
        status=ApprovalRequest.Status.PENDING,
        current_level=1,
        total_levels=len(levels),
        submitted_by=user,
        submitted_at=now,
        level_entered_at=now,
        created_by=user,
    )
    _log(request, action=ApprovalAction.Action.SUBMIT, user=user,
         level=1, level_name=levels[0].name, ctx=ctx)
    _fire('submitted', request)
    return request


@transaction.atomic
def approve(*, request_id, user, remarks='', ctx=None):
    """Record an approval; advance a level or finish the chain."""
    request = (
        ApprovalRequest.objects.select_for_update()
        .select_related('workflow', 'content_type')
        .get(pk=request_id)
    )
    level = _validate(request, user, ApprovalAction.Action.APPROVE, remarks)

    _log(request, action=ApprovalAction.Action.APPROVE, user=user,
         remarks=remarks, level=request.current_level,
         level_name=level.name, ctx=ctx)

    if _approvals_at_current_level(request) < level.min_approvals:
        # Quorum not met — the rung stays open for the remaining approvers.
        return request

    if request.current_level >= request.total_levels:
        request.status = ApprovalRequest.Status.APPROVED
        request.decided_at = timezone.now()
        request.save(update_fields=['status', 'decided_at', 'updated_at'])
        # Carried to the hook so a SAP post is attributed to the approver who
        # triggered it, not to "system".
        request._acting_user = user
        _fire('approved', request)
        return request

    request.current_level += 1
    request.level_entered_at = timezone.now()
    request.save(update_fields=['current_level', 'level_entered_at', 'updated_at'])
    return request


@transaction.atomic
def reject(*, request_id, user, remarks, ctx=None):
    """Reject. Remarks are MANDATORY — the orders flow accepts a blank reason
    (orders/views.py:2904, 3045), which leaves rejections unexplainable."""
    request = (
        ApprovalRequest.objects.select_for_update()
        .select_related('workflow', 'content_type')
        .get(pk=request_id)
    )
    level = _validate(request, user, ApprovalAction.Action.REJECT, remarks)

    _log(request, action=ApprovalAction.Action.REJECT, user=user,
         remarks=remarks, level=request.current_level,
         level_name=level.name, ctx=ctx)

    request.status = ApprovalRequest.Status.REJECTED
    request.decided_at = timezone.now()
    request.save(update_fields=['status', 'decided_at', 'updated_at'])
    _fire('rejected', request)
    return request


@transaction.atomic
def cancel(*, request_id, user, remarks='', ctx=None):
    """Creator withdraws the document before a decision."""
    request = (
        ApprovalRequest.objects.select_for_update()
        .select_related('workflow', 'content_type')
        .get(pk=request_id)
    )
    if request.status not in (ApprovalRequest.Status.DRAFT,
                              ApprovalRequest.Status.PENDING):
        raise ValidationError('Only a draft or pending request can be cancelled.')
    if request.submitted_by_id != user.id and not user.is_staff:
        raise PermissionDenied('Only the submitter may cancel this request.')

    _log(request, action=ApprovalAction.Action.CANCEL, user=user,
         remarks=remarks, level=request.current_level, ctx=ctx)
    request.status = ApprovalRequest.Status.CANCELLED
    request.decided_at = timezone.now()
    request.save(update_fields=['status', 'decided_at', 'updated_at'])
    _fire('cancelled', request)
    return request


def _validate(request, user, action, remarks):
    """Single shared validator for every decision path.

    Centralised so a bulk endpoint can never be laxer than the single one —
    the mistake tracker/services.py:163-217 explicitly avoids.
    """
    if request.status != ApprovalRequest.Status.PENDING:
        raise ValidationError(
            f'This request is {request.get_status_display().lower()}; no action possible.')

    level = _level_at(request, request.current_level)
    if level is None:
        raise ValidationError(
            'The workflow changed and this level no longer exists. '
            'Cancel and resubmit the document.')

    if action == ApprovalAction.Action.REJECT and not (remarks or '').strip():
        raise ValidationError('Remarks are mandatory when rejecting.')

    if request.workflow.forbid_self_approval and request.submitted_by_id == user.id:
        raise PermissionDenied('You cannot approve a document you submitted.')

    # The module-level grant, checked independently of can_act — this is the
    # path a real API call takes, so hiding the button in the UI is not enough.
    # CANCEL is exempt: withdrawing your own document is not an approval.
    if action != ApprovalAction.Action.CANCEL and not has_approve_permission(
            user, request.workflow.document_type):
        raise PermissionDenied(
            'You do not have permission to approve this type of document.')

    if user.id not in eligible_approver_ids(level, request.company):
        raise PermissionDenied(f'You are not an approver for "{level.name}".')

    already = request.actions.filter(
        round_number=request.round_number,
        level=request.current_level,
        approver=user,
        action=ApprovalAction.Action.APPROVE,
    ).exists()
    if already:
        raise ValidationError('You have already approved this level.')

    return level


def _actionable_pair_q(user):
    """Q matching (workflow, current_level) pairs this user may act on.

    Built by asking `eligible_approver_ids` about every active level, rather
    than re-deriving eligibility from roles here. That matters: named approvers
    NARROW a level, so a role-based shortcut would say a user can act on a rung
    that has been handed to someone else by name — which is exactly the bug
    where both approvers saw a level-1 document at the same time.

    `current_level` is a 1-based LADDER POSITION, not the level's `sequence`
    column, so the levels are ordered and enumerated. Matching on `sequence`
    breaks the moment a workflow's numbering does not start at 1 (see
    _level_at), and pairing with the workflow stops a level-2 grant in one
    workflow exposing level 2 of every other.
    """
    pair_q = Q()
    matched = False

    workflow_ids = (
        ApprovalLevel.objects.filter(is_active=True)
        .values_list('workflow_id', flat=True)
        .distinct()
    )
    for wf_id in workflow_ids:
        levels = list(
            ApprovalLevel.objects.filter(workflow_id=wf_id, is_active=True)
            .select_related('role')
            .order_by('sequence')
        )
        for position, level in enumerate(levels, start=1):
            if user.id in eligible_approver_ids(level):
                pair_q |= Q(workflow_id=wf_id, current_level=position)
                matched = True

    return pair_q if matched else None


def inbox_for(user):
    """Pending requests this user may act on RIGHT NOW, newest first."""
    pair_q = _actionable_pair_q(user)
    if pair_q is None:
        return ApprovalRequest.objects.none()

    qs = (
        ApprovalRequest.objects
        .filter(pair_q, status=ApprovalRequest.Status.PENDING)
        .select_related('workflow', 'content_type', 'submitted_by')
        .order_by('-created_at')
    )
    # Never show someone a document they cannot act on because they raised it.
    return qs.exclude(
        workflow__forbid_self_approval=True, submitted_by=user)


def actionable_request_ids(user):
    """IDs of PENDING requests awaiting this user — for filtering a wider list.

    Same rule as `inbox_for`, returned as ids so a caller can keep its own
    filters and simply exclude pending rows that belong to another level.
    """
    return set(inbox_for(user).values_list('id', flat=True))
