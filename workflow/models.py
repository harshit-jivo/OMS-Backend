"""Generic workflow engine — configuration, runtime, and the flow base.

Implements `docs/architecture/WORKFLOW_ENGINE_IMPLEMENTATION_PLAN.md`.

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


class CompanyScope(models.TextChoices):
    """Whether a configuration row applies to one company or to all of them."""

    ALL = 'ALL', 'All companies'
    SPECIFIC = 'SPECIFIC', 'A specific company'


class _CompanyScoped(models.Model):
    """`company_scope` + nullable `company`, with the invalid state barred.

    The point of `ALL` is that ONE row serves every company — the duplication
    JSAP forces (a copy per company for identical configuration) is what this
    removes.

    Why two columns rather than the `company = ''` sentinel the rest of the
    project uses (`approvals`, `orders.Scheme`, `payments` all do
    `Q(company=company) | Q(company='')`): `''` cannot be told apart from
    "nobody filled this in", so a row meaning ALL looks identical to a row
    left blank by accident. An explicit scope makes the intent a stated value,
    and the third CHECK below then makes the contradictory combination
    impossible to store.

    `company` is a CHECK-constrained closed string set rather than a foreign
    key, because there is no authoritative integer master for DOCUMENT company
    in this project — see `core.companies`.

    NOTE: this scope is applicability, NOT precedence. A `SPECIFIC` row does
    not outrank an `ALL` row. If both apply and both match, that is
    `AmbiguousWorkflowSelection` (plan §6.1). This is a deliberate difference
    from `approvals.services.resolve_workflow`, which orders by `-company` to
    prefer the company-specific row; that behaviour is left untouched there.
    """

    company_scope = models.CharField(
        max_length=8,
        choices=CompanyScope.choices,
        default=CompanyScope.ALL,
        help_text='ALL applies to every company; SPECIFIC to `company` only.',
    )
    company = models.CharField(
        max_length=20,
        choices=COMPANY_CHOICES,
        null=True,
        blank=True,
        help_text='NULL when scope is ALL; a company code when SPECIFIC.',
    )

    class Meta:
        abstract = True

    @classmethod
    def company_scope_constraints(cls, prefix):
        """The three CHECKs, named per concrete table.

        Returned rather than declared here because an abstract base cannot
        contribute uniquely-named constraints to several tables.
        """
        return [
            models.CheckConstraint(
                condition=Q(company_scope__in=list(CompanyScope.values)),
                name=f'{prefix}_company_scope_valid',
            ),
            models.CheckConstraint(
                condition=Q(company__isnull=True)
                | Q(company__in=list(COMPANY_CODES)),
                name=f'{prefix}_company_code_valid',
            ),
            # The one that matters: a row can never simultaneously mean
            # "specific company" and "all companies".
            models.CheckConstraint(
                condition=(
                    Q(company_scope=CompanyScope.ALL, company__isnull=True)
                    | Q(company_scope=CompanyScope.SPECIFIC,
                        company__isnull=False)
                ),
                name=f'{prefix}_company_scope_consistent',
            ),
        ]

    def applies_to_company(self, company):
        """Pure-Python mirror of the selection filter, for tests and admin."""
        if self.company_scope == CompanyScope.ALL:
            return True
        return bool(company) and self.company == company

    def clean(self):
        """Field-level guard so the CHECK is never how a user finds out."""
        from django.core.exceptions import ValidationError
        if self.company_scope == CompanyScope.ALL and self.company:
            raise ValidationError(
                {'company': 'Leave company empty when scope is ALL.'})
        if self.company_scope == CompanyScope.SPECIFIC and not self.company:
            raise ValidationError(
                {'company': 'A company is required when scope is SPECIFIC.'})


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class WorkflowModule(_Timestamped):
    """A business module that may use the engine (BUDGET, PRDO, ...).

    `business_table` / `business_key_column` / `flow_table` are what let the
    engine stay generic without a per-module branch in Python:

    * `business_key_column` names the attribute/column the engine binds when
      it tests a configured query against one document (plan §5.1). JSAP had
      to solve this per module — `id` here, `DocEntry` there — by hand.
    * `business_table` is the relation a configured query is allowed to read,
      and is the basis of the validator's allow-list (plan §14 Layer 2).
    * `flow_table` records where this module's runtime rows live, so a
      `(module, flow_id)` pair in `workflow_task` can be resolved back to a
      concrete row without the engine importing the module.
    """

    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=100)
    business_table = models.CharField(
        max_length=120,
        help_text='Schema-qualified relation the module\'s documents live in, '
                  'as written in raw SQL — e.g. '
                  '"workflow.workflow_test_document". Do NOT use Django\'s '
                  'db_table quoting form (schema"."table); a configured query '
                  'is raw SQL and would not parse.',
    )
    business_key_column = models.CharField(
        max_length=60,
        help_text='Column/attribute a configured query exposes for document '
                  'identity. Bound as a parameter, never interpolated.',
    )
    flow_table = models.CharField(
        max_length=120,
        help_text='Qualified relation holding this module\'s flow rows.',
    )
    #: Dotted `app_label.ModelName` of the concrete flow model. Resolved
    #: lazily through the app registry so the engine never imports a module.
    flow_model = models.CharField(
        max_length=120,
        blank=True,
        default='',
        help_text="Dotted 'app_label.ModelName' of the concrete flow model.",
    )

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

    `company_scope` is this workflow's APPLICABILITY rule — "does this
    workflow apply to a document from this company at all?" Keeping it here
    means applicability is readable off one row without inspecting the
    workflow's queries.

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

    class Meta:
        db_table = 'workflow"."workflows'
        ordering = ['module__code', 'code']
        verbose_name = 'Workflow'
        constraints = [
            models.UniqueConstraint(
                fields=['module', 'code'], name='workflow_module_code_uq',
            ),
            *_CompanyScoped.company_scope_constraints('workflow'),
        ]
        indexes = [
            models.Index(fields=['module'], name='workflow_module_idx'),
            # Serves the selection filter in workflow.services.selection.
            models.Index(fields=['module', 'company_scope', 'company'],
                         name='workflow_module_scope_idx'),
        ]

    def __str__(self):
        return f'{self.module.code}/{self.code}'


class WorkflowQuery(_Timestamped, _CompanyScoped):
    """A configured SELECT that decides whether a document enters a workflow.

    Preserves the JSAP `jsQuery` shape verified in plan §2.5 — `id`, `query`,
    `name`, `type`, `company`. Two notes carried from that audit:

    * `company` is an integer company id in JSAP (sharing `jsTemplate.company`,
      where 1 -> OIL). Here it is the code OMS documents actually carry
      ('OIL' / 'BEVERAGES' / 'MART'), plus an explicit `company_scope` so one
      row can serve ALL companies instead of being copied per company. The
      JSAP migration maps the integer to the code.
    * `type` is carried for parity ONLY. Its meaning could not be recovered
      from JSAP source — nothing in that codebase compares it to a literal, and
      it is written as an int but filtered as a string. Nothing in this engine
      reads it, so an unknown legacy value is inert rather than dangerous.

    `validated_at` is the execution gate: selection never runs a query whose
    `validated_at` is NULL (plan §5.3).
    """

    workflow = models.ForeignKey(
        Workflow, on_delete=models.CASCADE, related_name='queries',
    )
    name = models.CharField(max_length=120)
    query_text = models.TextField()
    type = models.CharField(
        max_length=30, blank=True, default='',
        help_text='JSAP parity only — nothing in the engine reads this.',
    )
    key_column = models.CharField(
        max_length=60, blank=True, default='',
        help_text="Overrides the module's business_key_column when this "
                  'query aliases its key differently.',
    )
    validated_at = models.DateTimeField(
        null=True, blank=True,
        help_text='Set by the validator. NULL means never executed.',
    )
    validation_error = models.TextField(blank=True, default='')

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
            *_CompanyScoped.company_scope_constraints('workflow_query'),
        ]
        indexes = [
            models.Index(fields=['workflow'], name='workflow_query_wf_idx'),
            models.Index(fields=['workflow', 'company_scope', 'company'],
                         name='workflow_query_scope_idx'),
        ]

    def __str__(self):
        return f'{self.workflow}: {self.name}'

    @property
    def effective_key_column(self):
        return self.key_column or self.workflow.module.business_key_column

    def scope_conflict(self):
        """Why this query's scope contradicts its workflow's, or None.

        A query may only NARROW its workflow's applicability:

            workflow ALL          -> any query scope
            workflow SPECIFIC(X)  -> query must be ALL or SPECIFIC(X)

        A query scoped to a different specific company than its workflow is a
        dead configuration that can never fire, so it is refused at
        validation time. This is a cross-row rule, which is why it is not a
        CHECK — a CHECK cannot read the parent row.
        """
        workflow = self.workflow
        if workflow.company_scope != CompanyScope.SPECIFIC:
            return None
        if self.company_scope != CompanyScope.SPECIFIC:
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
# Runtime
# ---------------------------------------------------------------------------

class TaskStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    APPROVED = 'APPROVED', 'Approved'
    REJECTED = 'REJECTED', 'Rejected'
    CANCELLED = 'CANCELLED', 'Cancelled'


class FlowStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    APPROVED = 'APPROVED', 'Approved'
    REJECTED = 'REJECTED', 'Rejected'
    CANCELLED = 'CANCELLED', 'Cancelled'


class ActionType(models.TextChoices):
    """Engine actions only.

    There is deliberately no RESUBMITTED member: resubmission is a module
    event, recorded in the module's own history, never here (plan §4.9.2).
    """

    SUBMIT = 'SUBMIT', 'Submit'
    APPROVE = 'APPROVE', 'Approve'
    REJECT = 'REJECT', 'Reject'
    CANCEL = 'CANCEL', 'Cancel'
    EXPIRE = 'EXPIRE', 'Expire'


class WorkflowTask(_Timestamped):
    """The one open action a stage owes. Cross-module, so the inbox spans apps.

    `flow_id` has NO database FK: the target table differs per module, so
    there is nothing single to point at. That referential integrity is the
    engine's responsibility and is covered by tests.

    `stage_user` stores the stage's CONFIGURED user, not the effective one.
    The replacement is applied at resolution time (`services.replacements`),
    which is what lets the original user automatically resume on an
    already-open task the moment a replacement window closes — writing the
    effective user here would require rewriting live rows instead.
    """

    module = models.ForeignKey(
        WorkflowModule, on_delete=models.PROTECT, related_name='tasks',
    )
    flow_id = models.BigIntegerField(
        help_text='Row id in the module\'s own flow table. Intentionally not '
                  'a FK — the target table varies by module.',
    )
    stage = models.ForeignKey(
        WorkflowStage, on_delete=models.PROTECT, related_name='tasks',
    )
    sequence = models.PositiveSmallIntegerField(
        help_text='Stage sequence, denormalised so the inbox needs no join.',
    )
    stage_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='workflow_tasks',
        help_text='The stage\'s CONFIGURED user. Effective actor is resolved '
                  'by date at read/action time.',
    )
    status = models.CharField(
        max_length=12, choices=TaskStatus.choices, default=TaskStatus.PENDING,
    )

    class Meta:
        db_table = 'workflow"."workflow_task'
        ordering = ['-created_at']
        verbose_name = 'Workflow Task'
        constraints = [
            # Partial on PENDING: at most one OPEN task per stage, while a
            # flow re-executed after a rejection can re-open a stage without
            # colliding with that stage's retained terminal rows (plan §4.6).
            models.UniqueConstraint(
                fields=['module', 'flow_id', 'stage'],
                condition=Q(status=TaskStatus.PENDING),
                name='workflow_task_one_open_per_stage_uq',
            ),
        ]
        indexes = [
            models.Index(
                fields=['stage_user', 'status'],
                condition=Q(status=TaskStatus.PENDING),
                name='workflow_task_inbox_idx',
            ),
            models.Index(fields=['module', 'flow_id'],
                         name='workflow_task_flow_idx'),
        ]

    def __str__(self):
        return f'task#{self.pk} {self.module_id}/{self.flow_id} {self.status}'


class WorkflowAction(models.Model):
    """Append-only engine history. No `updated_at`, no delete path.

    `stage_name` and `acted_by_username` are denormalised so history stays
    readable after configuration changes — the same reason
    `approvals.ApprovalAction` stores `level_name` and `approver_username`.

    `sequence` is monotonic per (module, flow_id) and continues across
    successive executions of the same flow, so a module that resubmits an
    entry gets one continuous engine history with no attempt counter.
    """

    module = models.ForeignKey(
        WorkflowModule, on_delete=models.PROTECT, related_name='actions',
    )
    flow_id = models.BigIntegerField()
    sequence = models.PositiveIntegerField(
        help_text='Monotonic per (module, flow_id), across all executions.',
    )
    stage = models.ForeignKey(
        WorkflowStage, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='actions',
    )
    stage_name = models.CharField(max_length=120, blank=True, default='')
    action = models.CharField(max_length=12, choices=ActionType.choices)
    acted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name='workflow_actions',
        help_text='Who actually acted.',
    )
    acted_by_username = models.CharField(max_length=150, blank=True, default='')
    on_behalf_of = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name='workflow_actions_on_behalf',
        help_text='The configured stage user, when a replacement was active.',
    )
    remarks = models.TextField(blank=True, default='')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=400, blank=True, default='')
    acted_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'workflow"."workflow_action'
        ordering = ['module', 'flow_id', 'sequence']
        verbose_name = 'Workflow Action'
        constraints = [
            models.UniqueConstraint(
                fields=['module', 'flow_id', 'sequence'],
                name='workflow_action_sequence_uq',
            ),
        ]
        indexes = [
            models.Index(fields=['module', 'flow_id', 'acted_at'],
                         name='workflow_action_flow_idx'),
        ]

    def __str__(self):
        return f'{self.module_id}/{self.flow_id} #{self.sequence} {self.action}'


# ---------------------------------------------------------------------------
# Module flow base
# ---------------------------------------------------------------------------

class WorkflowFlowBase(_Timestamped):
    """Workflow execution state for ONE business document.

    A module subclasses this and supplies the concrete document FK, which is
    what gives PostgreSQL real referential integrity while the engine stays
    generic:

        class BudgetFlow(WorkflowFlowBase):
            document = models.ForeignKey(
                'budget.BudgetDraft', on_delete=models.PROTECT,
                related_name='flows')

            class Meta(WorkflowFlowBase.Meta):
                abstract = False
                db_table = 'workflow"."budget_flow'

    The subclass MUST declare `UNIQUE(document)` — one flow row per document,
    reused across executions. The engine never creates a second flow row for
    a document, and never creates one because a document was resubmitted
    (plan §4.9.1). There is no `round_number`, no attempt counter, and no
    `total_stages` (derivable) or `current_user_id` (the actor is resolved
    from the current stage by date).
    """

    # Nullable because the MODULE owns this row and creates it before any
    # execution exists — the engine never creates a flow row itself, so a
    # document can legitimately have a flow row with no workflow selected
    # yet. `start()` sets it. A row with `current_stage` NULL has no
    # execution in progress, which is what `start()`'s guard tests.
    workflow = models.ForeignKey(
        Workflow, on_delete=models.PROTECT, null=True, blank=True,
        related_name='+',
    )
    matched_query = models.ForeignKey(
        WorkflowQuery, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='+',
        help_text='Which configured query selected this workflow.',
    )
    status = models.CharField(
        max_length=12, choices=FlowStatus.choices, default=FlowStatus.PENDING,
    )
    current_stage = models.ForeignKey(
        WorkflowStage, on_delete=models.PROTECT, null=True, blank=True,
        related_name='+',
    )
    current_sequence = models.PositiveSmallIntegerField(default=1)
    company = models.CharField(max_length=20, blank=True, default='')
    context_snapshot = models.JSONField(
        default=dict, blank=True,
        help_text='Routing inputs and selection explanation ONLY. Never a '
                  'copy of the business document.',
    )
    #: Inert until OD-4 is resolved. Declared because the approved flow-base
    #: column list includes it; nothing writes it while there is no outbox.
    integration_status = models.CharField(max_length=12, default='PENDING')
    lock_version = models.IntegerField(default=0)

    class Meta:
        abstract = True

    def __str__(self):
        return f'flow#{self.pk} {self.status} seq={self.current_sequence}'


# ---------------------------------------------------------------------------
# TestFlow — the first integration target (plan §16, §33)
# ---------------------------------------------------------------------------

class TestDocument(_Timestamped):
    """A deliberately minimal business document, for proving the engine.

    This exists so the generic engine can be driven end to end against an
    arbitrary concrete flow model BEFORE any production module is built. It is
    not a business table and nothing outside this app should reference it.
    """

    company = models.CharField(max_length=20, blank=True, default='')
    branch = models.CharField(max_length=50, blank=True, default='')
    department = models.CharField(max_length=50, blank=True, default='')
    document_number = models.CharField(max_length=50, blank=True, default='')
    amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    #: The module's own lifecycle log lives on the module side — see
    #: TestDocumentLog. `RESUBMITTED` is recorded there, never in
    #: workflow_action.
    status = models.CharField(max_length=20, default='DRAFT')

    class Meta:
        db_table = 'workflow"."workflow_test_document'
        ordering = ['-id']
        verbose_name = 'Workflow Test Document'
        indexes = [
            models.Index(fields=['company'], name='workflow_testdoc_comp_idx'),
        ]

    def __str__(self):
        return f'testdoc#{self.pk} {self.document_number or "-"}'


class TestDocumentLog(models.Model):
    """Module-side lifecycle history — the `flow_logs` role in the plan.

    Deliberately MODULE-owned, not engine-owned: it is where `RESUBMITTED`
    lives. The engine's own `WorkflowAction` has no such action and must never
    gain one (plan §4.9.2).

    Named for this harness rather than `flow_logs` because no `flow_logs`
    table exists anywhere in OMS-Backend — the plan records that the name is
    the planned module-side term, not an existing one. A real module should
    use whatever its own architecture settles on.
    """

    class Event(models.TextChoices):
        SUBMITTED = 'SUBMITTED', 'Submitted'
        STAGE_APPROVED = 'STAGE_APPROVED', 'Stage approved'
        REJECTED = 'REJECTED', 'Rejected'
        RESUBMITTED = 'RESUBMITTED', 'Resubmitted'
        APPROVED = 'APPROVED', 'Approved'

    document = models.ForeignKey(
        TestDocument, on_delete=models.CASCADE, related_name='logs',
    )
    sequence = models.PositiveIntegerField()
    event = models.CharField(max_length=20, choices=Event.choices)
    remarks = models.TextField(blank=True, default='')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'workflow"."workflow_test_document_log'
        ordering = ['document', 'sequence']
        verbose_name = 'Workflow Test Document Log'
        constraints = [
            models.UniqueConstraint(
                fields=['document', 'sequence'],
                name='workflow_testdoclog_seq_uq',
            ),
        ]

    def __str__(self):
        return f'doc#{self.document_id} #{self.sequence} {self.event}'


class TestFlow(WorkflowFlowBase):
    """Concrete flow model for `TestDocument`.

    `UNIQUE(document)` is the constraint that prevents a business entry from
    ever having two simultaneous workflow executions. One row per document,
    reused — a rejection followed by a module-driven resubmission re-uses this
    same row (plan §4.9.1).
    """

    document = models.ForeignKey(
        TestDocument, on_delete=models.PROTECT, related_name='flows',
    )

    class Meta:
        db_table = 'workflow"."workflow_test_flow'
        ordering = ['-id']
        verbose_name = 'Workflow Test Flow'
        constraints = [
            models.UniqueConstraint(
                fields=['document'], name='workflow_testflow_document_uq',
            ),
            models.CheckConstraint(
                condition=Q(current_sequence__gte=1),
                name='workflow_testflow_seq_positive',
            ),
        ]
        indexes = [
            models.Index(
                fields=['status', 'current_stage'],
                condition=Q(status=FlowStatus.PENDING),
                name='workflow_testflow_pending_idx',
            ),
        ]
