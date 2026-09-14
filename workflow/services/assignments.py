"""Who is responsible for a stage — reading it, and changing it.

THE ONE IDEA THIS MODULE EXISTS TO PROTECT
------------------------------------------
The STAGE is the stable workflow step. `workflow_stages.user_id` is the
CURRENT configured actor for that step — not a copy handed out to each entry
that passes through it.

So changing a stage's user is a one-row UPDATE and nothing else:

    Stage 2  Mukesh -> Ravi

    same workflow, same stage, same entries already waiting there,
    new responsible user.

There is deliberately no "reassign pending entries" operation anywhere in this
codebase, and adding one would be a bug rather than a feature. A module's task
row points at `stage_id`; responsibility is resolved from the stage's CURRENT
configuration every time it is asked. Copying the user onto each task at
creation time is what makes a reassignment step necessary, and it is exactly
what modules are told not to do — see
`docs/architecture/WORKFLOW_MODULE_INTEGRATION.md`.

Two kinds of change, deliberately different:

* **Permanent** — `workflow_stages.user_id` changes on ONE stage. Mukesh no
  longer works that stage. This module only reads; the write is an ordinary
  `PATCH /stages/<id>/`, one stage at a time and never in bulk.
* **Temporary** — a `WorkflowUserReplacement` row. The stage keeps Mukesh;
  only the EFFECTIVE actor changes, and only for a date window. Nothing here
  writes to `workflow_stages` for that case. See `services/replacements.py`.

Neither ever touches history. A past approval says who actually approved; the
current configuration says who would approve next. Conflating them would
rewrite the audit trail every time somebody changed jobs.
"""
from dataclasses import dataclass

from workflow.models import WorkflowStage
from workflow.services import replacements


@dataclass(frozen=True)
class StageAssignment:
    """One stage, its owning configuration, and who is responsible for it.

    `configured_user_id` is what an administrator set. `effective_user_id` is
    who may act on the given date, after temporary replacements. They are
    reported separately because they answer different questions, and a UI that
    showed only one of them could not explain why somebody unexpected is
    holding the work.
    """

    stage_id: int
    sequence: int
    stage_name: str
    workflow_id: int
    workflow_code: str
    workflow_name: str
    company: str
    module_id: int
    module_code: str
    module_name: str
    configured_user_id: int
    configured_username: str
    effective_user_id: int
    effective_username: str

    @property
    def has_active_replacement(self):
        return self.effective_user_id != self.configured_user_id

    def as_dict(self):
        return {
            'stage_id': self.stage_id,
            'sequence': self.sequence,
            'stage_name': self.stage_name,
            'workflow_id': self.workflow_id,
            'workflow_code': self.workflow_code,
            'workflow_name': self.workflow_name,
            'company': self.company,
            'module_id': self.module_id,
            'module_code': self.module_code,
            'module_name': self.module_name,
            'configured_user_id': self.configured_user_id,
            'configured_username': self.configured_username,
            'effective_user_id': self.effective_user_id,
            'effective_username': self.effective_username,
            'has_active_replacement': self.has_active_replacement,
        }


def _base_queryset():
    """Stages with their workflow and module joined, in reading order.

    One query with two joins — `workflow_stages -> workflows ->
    workflow_modules`. Nothing about any business module's documents or tasks
    is read: an administrator looking at assignments must not cause every
    module to load its runtime.
    """
    return (
        WorkflowStage.objects
        .select_related('user', 'workflow', 'workflow__module')
        .order_by('workflow__module__code', 'workflow__code', 'sequence')
    )


def _build(stage, effective_map):
    effective_id = effective_map.get(stage.user_id, stage.user_id)
    effective_name = (stage.user.username if effective_id == stage.user_id
                      else effective_map.get(f'name:{effective_id}', ''))
    return StageAssignment(
        stage_id=stage.pk,
        sequence=stage.sequence,
        stage_name=stage.name,
        workflow_id=stage.workflow_id,
        workflow_code=stage.workflow.code,
        workflow_name=stage.workflow.name,
        company=stage.workflow.company,
        module_id=stage.workflow.module_id,
        module_code=stage.workflow.module.code,
        module_name=stage.workflow.module.name,
        configured_user_id=stage.user_id,
        configured_username=stage.user.username,
        effective_user_id=effective_id,
        effective_username=effective_name,
    )


def _effective_map(stages, on_date):
    """`{configured_id: effective_id}` plus `{'name:<id>': username}`.

    One replacement query for the whole page and one user query for the
    stand-in names, rather than two per row.
    """
    from django.contrib.auth import get_user_model

    configured = {stage.user_id for stage in stages}
    if not configured:
        return {}
    mapping = replacements.effective_user_ids(configured, on_date)
    stand_ins = {new for old, new in mapping.items() if new != old}
    if stand_ins:
        for pk, username in (get_user_model().objects
                             .filter(pk__in=stand_ins)
                             .values_list('pk', 'username')):
            mapping[f'name:{pk}'] = username
    return mapping


def assignments_for_user(user_id, *, on_date=None, include_inactive=False):
    """Every workflow stage this user is the CONFIGURED actor for.

    The answer to "what would I break by moving this person?", and the data
    behind the Stages tab's By User view. Derived by joining the three
    configuration tables — there is no assignment table and there must not be
    one, because `workflow_stages.user_id` already IS the assignment.
    """
    qs = _base_queryset().filter(user_id=user_id)
    if not include_inactive:
        qs = qs.filter(is_active=True)
    stages = list(qs)
    effective = _effective_map(stages, on_date or replacements.today())
    return [_build(stage, effective) for stage in stages]


def get_stage_assignment(stage_id, effective_date=None):
    """One stage's assignment — the call a business module makes.

    A module holds `stage_id` on its own task row and asks this who is
    responsible right now. That indirection is the whole point: the answer
    changes when configuration changes, with no write to the module's rows.

    Returns `None` when the stage does not exist, so a module can tell
    "deleted configuration" apart from "nobody assigned" (which cannot happen:
    `user` is NOT NULL).
    """
    stage = _base_queryset().filter(pk=stage_id).first()
    if stage is None:
        return None
    on_date = effective_date or replacements.today()
    return _build(stage, _effective_map([stage], on_date))


# `reassign_user()` — a bulk "move every stage from A to B" — used to sit here
# and has been removed. Moving somebody's work is done one stage at a time, by
# writing `WorkflowStage.user`, so that the change and the thing being looked
# at are the same size. A single call that rewrote every stage a person owned
# across every module had no natural review step and no undo.


def assignment_counts(user_ids, *, include_inactive=False):
    """`{user_id: number of stages}` for a set of users, in one query."""
    from django.db.models import Count

    qs = WorkflowStage.objects.filter(user_id__in=list(user_ids))
    if not include_inactive:
        qs = qs.filter(is_active=True)
    return dict(
        qs.values_list('user_id')
        .annotate(n=Count('id'))
        .values_list('user_id', 'n')
    )
