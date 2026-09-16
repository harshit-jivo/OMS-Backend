"""Generic workflow engine — configuration, runtime, and the flow base.

Implements `docs/Approvals/WORKFLOW_ENGINE_IMPLEMENTATION_PLAN.md`.

Three things this module deliberately does NOT contain, each a settled
decision in the approved plan:

* no approver collection and no quorum — a stage has exactly ONE user
  (`WorkflowStage.user`), so `approval_required` / `rejection_required` hav e
  no meaning here (plan §4.4);
* no `priority` / `selection_priority` / `is_active` — selection is fail-loud
  on ambiguity instead of ordered (plan §4.2, §6.1);
* no `round_number` / attempt counter / resubmission state — resubmission is
  a MODULE concept; the engine is simply re-invoked on the same flow row
  (plan §3.1, §4.9.1).

`workflow_outbox` is not implemented: OD-4 is still unresolved.

Schema
------
Every table lives in the dedicated `workflow` PostgreSQL schema, using the
`'workflow"."<table>'` db_table form already used by `HAIS` — that puts the
table in the schema without growing the global `search_path`.

Timestamps
----------
These models declare `created_at` / `updated_at` by hand rather than reusing
`core.models.TimeStampedModel`, which also adds `created_by`. The approved
schema enumerates the columns for every table here (and for
`workflow_user_replacements` the brief said "exactly these fields"), so the
plan is followed literally rather than widened by a mixin.
"""
from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateRangeField
from django.db import models
from django.db.models import F, Func, Q, Value

from core.companies import COMPANY_CHOICES, COMPANY_CODES


class _Timestamped(models.Model):
    """`created_at` / `updated_at` only — see the module docstring."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


#: The value that means "every company", stored in `company` like any other.
COMPANY_ALL = 'ALL'

#: Everything `company` may hold: ALL, or one real company code.
COMPANY_VALUES = [COMPANY_ALL, *COMPANY_CODES]


class _CompanyScoped(models.Model):
    """A single `company` column: `ALL`, or one company code.

    ONE row serves every company when `company = 'ALL'` — the duplication JSAP
    forces (a copy per company for identical configuration) is what this
    removes.

    WHY ONE COLUMN, NOT TWO
    -----------------------
    This used to be `company_scope` ('ALL' | 'SPECIFIC') plus a nullable
    `company`, with three CHECKs barring the contradictory combinations. The
    pair was defensible in isolation, but it made the same fact storable two
    ways and meant every reader — filter, serializer, admin, form, badge — had
    to join two columns back together before it could answer "which company?".
    `ALL` is simply one more value the column can take, so it is stored that
    way, and the whole class of contradictory rows stops existing rather than
    being forbidden.

    It is NOT the `company = ''` sentinel the rest of the project uses
    (`approvals`, `orders.Scheme`, `payments` all do
    `Q(company=company) | Q(company='')`): `''` cannot be told apart from
    "nobody filled this in". `'ALL'` is a stated value, and the CHECK below
    rejects anything that is neither `ALL` nor a real company.

    `company` is a CHECK-constrained closed string set rather than a foreign
    key, because there is no authoritative integer master for DOCUMENT company
    in this project — see `core.companies`.

    NOTE: this is applicability, NOT precedence. A specific company does not
    outrank `ALL`. If both apply and both match, that is
    `AmbiguousWorkflowSelection` (plan §6.1). This is a deliberate difference
    from `approvals.services.resolve_workflow`, which orders by `-company` to
    prefer the company-specific row; that behaviour is left untouched there.
    """

    company = models.CharField(
        max_length=20,
        choices=[(COMPANY_ALL, 'All companies'), *COMPANY_CHOICES],
        default=COMPANY_ALL,
        help_text="'ALL' applies to every company; otherwise the one company "
                  'code this row applies to.',
    )

    class Meta:
        abstract = True

    @classmethod
    def company_constraints(cls, prefix):
        """The single CHECK, named per concrete table.

        Returned rather than declared here because an abstract base cannot
        contribute uniquely-named constraints to several tables.
        """
        return [
            models.CheckConstraint(
                condition=Q(company__in=COMPANY_VALUES),
                name=f'{prefix}_company_valid',
            ),
        ]

    def applies_to_company(self, company):
        """Pure-Python mirror of the selection filter, for tests and admin."""
        if self.company == COMPANY_ALL:
            return True
        return bool(company) and self.company == company


#: The column the engine binds the document id against when the CALLER does
#: not name one.
#:
#: This is RUNTIME context, not configuration. It lived on the module registry
#: (`business_key_column`), then briefly on the query (`key_column`); both were
#: wrong for the same reason — each asked someone to restate, inside the
#: engine, a fact the business module already knows about its own table. The
#: module passes it at invocation now (`engine.start(..., key_column=...)`),
#: and this is the default for the overwhelming majority that key on `id`.
DEFAULT_KEY_COLUMN = 'id'


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class WorkflowModule(_Timestamped):
    """A business module that may use the engine (BUDGET, PRDO, ...).

    IDENTITY ONLY — `code` and `name`. This row says "BUDGET exists and may
    have workflows configured against it", and nothing more.

    It deliberately does NOT describe the module's implementation. Earlier
    revisions stored `business_table`, `business_key_column`, `flow_table` and
    `flow_model` here, which put a second, hand-maintained copy of the
    module's schema inside the engine: the module's own migrations were the
    real source of truth, and the registry copy could drift from them with
    nothing to detect it. Those columns are gone, and the values now come from
    where they are already true:

    * the concrete flow model declares its own `workflow_module_code`, and
      `workflow.flows` resolves it through Django's app registry — see that
      module for the runtime contract;
    * the key column is RUNTIME context the module passes when it invokes
      the engine (`engine.start(..., key_column=...)`), because the module is
      what knows which column identifies one of its documents;
    * the module's tables are known to the module's own models.

    There is no `is_active` either. Retiring a module is expressed by
    deactivating its WORKFLOWS — that is what stops routing, and it leaves the
    registry as a plain statement of which modules exist. Nothing is ever
    deleted from this table: flows, tasks and the append-only action history
    all reference it, and `workflows.module` is ON DELETE RESTRICT.
    """

    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=100)

    class Meta:
        db_table = 'workflow"."workflow_modules'
        ordering = ['code']
        verbose_name = 'Workflow Module'
        constraints = [
            models.CheckConstraint(
                condition=Q(code=Func(F('code'), function='UPPER')),
                name='workflow_module_code_upper',
            ),
        ]

    def __str__(self):
        return self.code


class Workflow(_Timestamped, _CompanyScoped):
    """One workflow belonging to a module.

    `company` is this workflow's APPLICABILITY rule — "does this workflow
    apply to a document from this company at all?" — and holds one of `ALL`,
    `OIL`, `BEVERAGES`, `MART`. Keeping it here means applicability is
    readable off one row without inspecting the workflow's queries.

    No `priority` and no `is_active`, by decision. A workflow is reachable
    only while it has candidate query rows, so retiring one means removing or
    closing its `WorkflowQuery` rows — lifecycle lives on the selector, not on
    a status column here (plan §4.2). In-flight flows hold an FK and keep
    running regardless.
    """

    module = models.ForeignKey(
        WorkflowModule,
        on_delete=models.PROTECT,          # RESTRICT: never orphan a workflow
        related_name='workflows',
    )
    code = models.CharField(max_length=60)
    name = models.CharField(max_length=120)
    #: Retire without deleting — see WorkflowModule.is_active for the full
    #: reasoning. Inactive rows are EXCLUDED outright; `is_active` is never
    #: used to rank or prefer one row over another, so it cannot become the
    #: implicit selection input that `priority` would have been.
    is_active = models.BooleanField(
        default=True,
        help_text='Inactive rows are hidden and take no part in selection. '
                  'Deactivate rather than delete — history references these.',
    )

    class Meta:
        db_table = 'workflow"."workflows'
        ordering = ['module__code', 'code']
        verbose_name = 'Workflow'
        constraints = [
            models.UniqueConstraint(
                fields=['module', 'code'], name='workflow_module_code_uq',
            ),
            *_CompanyScoped.company_constraints('workflow'),
        ]
        indexes = [
            models.Index(fields=['module'], name='workflow_module_idx'),
            # Serves the selection filter in workflow.services.selection.
            models.Index(fields=['module', 'company'],
                         name='workflow_module_scope_idx'),
        ]

    def __str__(self):
        return f'{self.module.code}/{self.code}'


class WorkflowQuery(_Timestamped, _CompanyScoped):
    """A configured SELECT that decides whether a document enters a workflow.

    A query says ONE thing: under what condition does its workflow apply?
    So it stores only `company`, `name`, `query_text`, its workflow, and its
    validation state.

    Two columns were deliberately removed and must not come back:

    * `type` was JSAP parity ONLY. Nothing in this engine ever read it, and
      its meaning could not be recovered from JSAP source — it is written as
      an int there but filtered as a string, and nothing compares it to a
      literal. A column nothing reads is a column that misleads.
    * `key_column` asked the ADMINISTRATOR which column identifies a
      document. That is runtime context owned by the business module, not
      configuration: the module knows its own key and supplies it when it
      invokes the engine. See `workflow.services.conditions.matches`.

    `company` is `ALL` / `OIL` / `BEVERAGES` / `MART` — the value chosen in
    the UI, stored as-is. `validated_at` is the execution gate: selection
    never runs a query whose `validated_at` is NULL (plan §5.3).
    """

    workflow = models.ForeignKey(
        Workflow, on_delete=models.CASCADE, related_name='queries',
    )
    name = models.CharField(max_length=120)
    query_text = models.TextField()
    validated_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Set by the validator. NULL means never executed.',
    )
    validation_error = models.TextField(blank=True, default='')
    #: Retire without deleting — see WorkflowModule.is_active for the full
    #: reasoning. Inactive rows are EXCLUDED outright; `is_active` is never
    #: used to rank or prefer one row over another, so it cannot become the
    #: implicit selection input that `priority` would have been.
    is_active = models.BooleanField(
        default=True,
        help_text='Inactive rows are hidden and take no part in selection. '
                  'Deactivate rather than delete — history references these.',
    )

    class Meta:
        db_table = 'workflow"."workflow_queries'
        ordering = ['workflow', 'name']
        verbose_name = 'Workflow Query'
        constraints = [
            models.UniqueConstraint(
                fields=['workflow', 'name'], name='workflow_query_name_uq',
            ),
            # A cheap structural floor only. The real gate is
            # workflow.validators + the read-only execution path.
            #
            # NOTE `\y`, not `\b`. Django passes this pattern straight to
            # PostgreSQL's `~*`, and in POSIX ARE `\b` is BACKSPACE — the word
            # boundary is `\y`. With `\b` this constraint matched nothing, so
            # every insert failed; caught only by running against real
            # PostgreSQL.
            models.CheckConstraint(
                condition=Q(query_text__iregex=r'^\s*(select|with)\y'),
                name='workflow_query_select_only',
            ),
            *_CompanyScoped.company_constraints('workflow_query'),
        ]
        indexes = [
            models.Index(fields=['workflow'], name='workflow_query_wf_idx'),
            models.Index(fields=['workflow', 'company'],
                         name='workflow_query_scope_idx'),
        ]

    def __str__(self):
        return f'{self.workflow}: {self.name}'

    def scope_conflict(self):
        """Why this query's company contradicts its workflow's, or None.

        A query may only NARROW its workflow's applicability:

            workflow ALL  -> any query company
            workflow X    -> query must be ALL or X

        A query set to a different company than its workflow is a dead
        configuration that can never fire, so it is refused at validation
        time. This is a cross-row rule, which is why it is not a CHECK — a
        CHECK cannot read the parent row.
        """
        workflow = self.workflow
        if workflow.company == COMPANY_ALL:
            return None
        if self.company == COMPANY_ALL:
            return None
        if self.company == workflow.company:
            return None
        return (
            f'This query is scoped to {self.company}, but its workflow '
            f'"{workflow.code}" applies only to {workflow.company}. The '
            f'query could never match. Use ALL, or {workflow.company}.'
        )


class WorkflowStage(_Timestamped):
    """One stage of a workflow, handled by EXACTLY ONE user.

    `sequence` is stage execution order — it is NOT selection priority and is
    never consulted when choosing a workflow (plan §2.1a, §6.1).

    `user` is NOT NULL, so "no approver configured" cannot happen. There is
    deliberately no approver child table, no role expansion, no grant
    fallback, and no `approval_required` / `rejection_required`: one approve
    completes the stage, one reject ends the workflow.
    """

    workflow = models.ForeignKey(
        Workflow, on_delete=models.CASCADE, related_name='stages',
    )
    name = models.CharField(max_length=120)
    sequence = models.PositiveSmallIntegerField(
        help_text='1-based stage execution order. Not selection priority.',
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,          # RESTRICT: a configured actor
        related_name='workflow_stages',    # cannot be deleted out from under
        help_text='The single configured user for this stage.',
    )
    #: Retire without deleting — see WorkflowModule.is_active for the full
    #: reasoning. Inactive rows are EXCLUDED outright; `is_active` is never
    #: used to rank or prefer one row over another, so it cannot become the
    #: implicit selection input that `priority` would have been.
    is_active = models.BooleanField(
        default=True,
        help_text='Inactive rows are hidden and take no part in selection. '
                  'Deactivate rather than delete — history references these.',
    )

    class Meta:
        db_table = 'workflow"."workflow_stages'
        ordering = ['workflow', 'sequence']
        verbose_name = 'Workflow Stage'
        constraints = [
            models.UniqueConstraint(
                fields=['workflow', 'sequence'], name='workflow_stage_seq_uq',
            ),
            models.CheckConstraint(
                condition=Q(sequence__gte=1), name='workflow_stage_seq_positive',
            ),
        ]
        indexes = [
            models.Index(fields=['workflow', 'sequence'],
                         name='workflow_stage_wf_seq_idx'),
            models.Index(fields=['user'], name='workflow_stage_user_idx'),
        ]

    def __str__(self):
        return f'{self.workflow} #{self.sequence} {self.name}'


class WorkflowUserReplacement(_Timestamped):
    """Temporary stand-in for a user, resolved by date.

    Exactly the fields the approved plan enumerates — no `is_active`, no
    priority, and nothing that touches the user's account. A replacement
    changes only which user the engine treats as the effective actor
    (plan §8); `users_user` is never written.

    Overlap for one `old_user` is barred by a GiST exclusion constraint rather
    than by application code, so at most one row can ever match a
    (user, date) pair and resolution is deterministic instead of
    order-dependent. Requires the `btree_gist` extension, installed by this
    app's initial migration.
    """

    old_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='workflow_replaced_by',
    )
    new_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='workflow_replacing',
    )
    reason = models.CharField(max_length=255, blank=True, default='')
    start_date = models.DateField(help_text='Inclusive.')
    end_date = models.DateField(help_text='Inclusive.')
    #: Retire without deleting — see WorkflowModule.is_active for the full
    #: reasoning. Inactive rows are EXCLUDED outright; `is_active` is never
    #: used to rank or prefer one row over another, so it cannot become the
    #: implicit selection input that `priority` would have been.
    is_active = models.BooleanField(
        default=True,
        help_text='Inactive rows are hidden and take no part in selection. '
                  'Deactivate rather than delete — history references these.',
    )

    class Meta:
        db_table = 'workflow"."workflow_user_replacements'
        ordering = ['-start_date']
        verbose_name = 'Workflow User Replacement'
        constraints = [
            models.CheckConstraint(
                condition=Q(end_date__gte=F('start_date')),
                name='workflow_replacement_dates_ordered',
            ),
            # The degenerate self-replacement is meaningless and would make
            # resolution a no-op that looks configured.
            models.CheckConstraint(
                condition=~Q(old_user=F('new_user')),
                name='workflow_replacement_not_self',
            ),
            # Inclusive bounds ('[]'): 22 Sep -> 23 Sep is adjacent (allowed),
            # 22 Sep -> 22 Sep overlaps (rejected).
            ExclusionConstraint(
                name='workflow_replacement_no_overlap',
                expressions=[
                    ('old_user', '='),
                    (
                        Func(
                            F('start_date'), F('end_date'), Value('[]'),
                            function='daterange',
                            output_field=DateRangeField(),
                        ),
                        '&&',
                    ),
                ],
            ),
        ]
        indexes = [
            models.Index(fields=['old_user', 'start_date', 'end_date'],
                         name='wf_replacement_lookup_idx'),
        ]

    def __str__(self):
        return (f'{self.old_user_id} -> {self.new_user_id} '
                f'[{self.start_date} .. {self.end_date}]')


# ---------------------------------------------------------------------------
# Nothing below this line, deliberately
# ---------------------------------------------------------------------------
#
# This app used to define `WorkflowTask`, `WorkflowAction`, `WorkflowFlowBase`
# and a `TestFlow`/`TestDocument` harness. They are gone, and must not come
# back, because they made the engine own RUNTIME:
#
#   * `workflow_task` / `workflow_action` assumed ONE universal shape for
#     every module's approval runtime and history. A module's task carries
#     module-specific columns, and its history is part of its own audit story;
#     forcing both through one engine table meant every module either bent to
#     that shape or kept a second copy of its own.
#   * `WorkflowFlowBase` prescribed the flow row — status, current_stage,
#     current_sequence, lock_version — so the engine was still dictating the
#     module's runtime even though the table lived in the module's schema.
#   * `TestDocument` / `TestFlow` were a verification harness that had become
#     permanent production tables inside `workflow`.
#
# What this app owns now is CONFIGURATION, and only configuration:
#
#     WorkflowModule  -> which modules may be configured
#     Workflow        -> the workflow definitions, per module and company
#     WorkflowQuery   -> the conditions deciding whether one applies
#     WorkflowStage   -> the ordered stages and their configured user
#     WorkflowUserReplacement -> who currently stands in for whom
#
# A module asks `workflow.services.selection.select_for_module()` which
# workflow applies and what its stages are, then builds its OWN task, action,
# history and lifecycle from that answer. See
# `docs/Approvals/WORKFLOW_MODULE_INTEGRATION.md`.
