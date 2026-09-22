"""BackDate (BKDT) — temporary back-posting rights in SAP.

WHAT THIS MODULE IS
-------------------
A user asks for permission to post documents of one type, in one company, with
a posting date inside a bounded past window. The request is approved through a
configured chain, and the final approval writes the grant into SAP's HANA
database (`OPEN_BKDT`).

OWNERSHIP — WHY THESE THREE TABLES EXIST HERE AND NOT IN `workflow`
--------------------------------------------------------------------
The Workflow Engine answers exactly one question: *which workflow applies to
this document, and what stages/users are configured?* Everything that happens
afterwards is this module's business, and lives in this schema:

    BackDate            the business document
    BackDateFlow        where it currently is, and the SAP outcome
    BackDateActionLog   what was decided or changed, by whom, when

There is deliberately no `workflow_task` / `workflow_action` in the engine to
inherit from, and no task table here either — see `BackDateFlow`.

THE ONE LOAD-BEARING LINK: `BackDateFlow.current_stage`
-------------------------------------------------------
A flow points at the ENGINE's stage. Who must act is resolved from that stage
on every read, through
`workflow.services.assignments.get_stage_assignment(stage_id)`.

That indirection is the whole point. When an administrator changes who works a
stage, requests already waiting there follow the new user with nothing copied,
moved or rewritten. `current_user` is a denormalised convenience for display
and for cheap filtering — never the authority. See its field comment.

MIGRATION NOTE
--------------
This is a fresh module. No JSAP BKDT transactional data is imported, and no
table here carries a JSAP id — JSAP keeps its own history for reference. The
BEHAVIOUR is migrated; the rows are not.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q

from core.companies import (
    COMPANY_CHOICES,
    COMPANY_CODES,
    COMPANY_SET_SEPARATOR,
    canonical_company_set,
    company_set_labels,
    company_set_values,
)


def _t(table):
    """Schema-qualify a table for the dedicated `backdate` Postgres schema.

    The `schema"."table` form puts the table in its own schema without growing
    the global `search_path` — the same trick `HAIS` and `workflow` use.
    """
    return f'backdate"."{table}'


#: JSAP identifies companies by the numeric branch codes 1/2/3. They are
#: translated HERE, at the module edge, and the codes never travel further —
#: not into the workflow engine, and not into HANA (which wants the name).
JSAP_BRANCH_TO_COMPANY = {'1': 'OIL', '2': 'BEVERAGES', '3': 'MART'}


class RequestAction(models.TextChoices):
    """What the granted rights are for.

    JSAP stores this, displays it, and then never sends it to HANA — the model
    property that would have carried it is commented out in the source. It is
    kept here because an approver is agreeing to one or the other, and a grant
    whose scope is not recorded cannot be audited.
    """

    ADD = 'A', 'Add'
    UPDATE = 'U', 'Update'


#: What `BackDate.action` may hold.
#:
#: A request can ask for BOTH, and JSAP stored exactly that — `'A,U'` in one
#: `userDocument` row — because SAP never receives the action at all:
#: `OPEN_BKDT` has no such parameter. So asking for Add and Update is one grant
#: with a wider recorded scope, NOT two grants. Splitting it would write two
#: rows to SAP identical in every column, which is duplicate noise in a table
#: SAP's posting validator scans on every document.
ACTION_VALUES = [RequestAction.ADD, RequestAction.UPDATE, 'A,U']

ACTION_LABELS = {
    RequestAction.ADD: 'Add',
    RequestAction.UPDATE: 'Update',
    'A,U': 'Add and Update',
}


def normalise_action(value):
    """`'U,A'` and `'a , u'` both mean `'A,U'`; anything else is returned as is.

    JSAP stored whichever order the user happened to tick (`'A,U'` 821 times,
    `'U,A'` 150), which makes the same request look like two different ones.
    One spelling per meaning here.
    """
    parts = [p.strip().upper() for p in str(value or '').split(',') if p.strip()]
    ordered = [a for a in (RequestAction.ADD, RequestAction.UPDATE)
               if a in parts]
    return ','.join(ordered) if ordered else str(value or '').strip().upper()


def normalise_company(value):
    """The chosen companies, as one canonical value.

    Returns `(canonical, error)`. A list is accepted — a form sends the ticked
    boxes — and so is an already-joined string. `OIL,MART` and `MART,OIL` are
    the same request, so both normalise to `OIL,MART`: one set, one spelling,
    or the column would hold two values meaning the same thing.

    ONE REQUEST MAY NAME SEVERAL COMPANIES, and that is the point. The rights
    asked for are the same rights in two or three SAP databases, decided once
    by the same approvers; making that two requests made one decision into two
    approval chains that could disagree. The SAP write still happens once per
    company — see `services/hana.apply_grant`, which has always fanned out —
    so the failure JSAP had (a branch loop with no per-branch record) is
    answered by recording every call, not by forbidding the request.

    Thin on purpose: `core.companies.canonical_company_set` is the shared
    definition, and this stays as the name the rest of this app already calls.
    """
    return canonical_company_set(value)


class FlowStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    APPROVED = 'APPROVED', 'Approved'
    REJECTED = 'REJECTED', 'Rejected'


class HanaStatus(models.TextChoices):
    """The SAP write either happened or it did not.

    Two values, and NULL before the write is attempted. An earlier version had
    four (`NA`/`PENDING`/`APPLIED`/`FAILED`), which made "has SAP been written
    to?" a question with three possible no-answers. NULL says "not yet" once.
    """

    SUCCESS = 'SUCCESS', 'Success'
    FAILED = 'FAILED', 'Failed'


class LogAction(models.TextChoices):
    """The BackDate lifecycle, and nothing else.

    No SUBMIT: creation and submission are one transaction, so a separate row
    would record the same instant twice. No HANA_* rows either — the SAP
    outcome is a property of the flow (`hana_status`), not an act a person
    took, and duplicating it here would let the two disagree.
    """

    CREATE = 'CREATE', 'Created'
    UPDATE = 'UPDATE', 'Updated'
    APPROVE = 'APPROVE', 'Approved'
    REJECT = 'REJECT', 'Rejected'


class BackDate(models.Model):
    """One request for back-posting rights: one or more companies, one
    document type.

    ONE REQUEST, ONE APPROVAL CHAIN, ONE SAP CALL PER COMPANY. A person asking
    for the same rights in OIL and MART is making one request and one
    decision, so there is one row here, one flow, one workflow and one
    history. Only the SAP write fans out, in `services/hana.apply_grant`.

    A SET IS NOT A TRANSACTION, and nothing here pretends otherwise. SAP gives
    no cross-schema transaction, so OIL can land while MART fails. That is
    recorded rather than hidden: `hana_status` is FAILED unless every company
    succeeded, and `hana_status_text` keeps each company's exact answer. JSAP
    looped branches the same way but kept no per-branch record, so a partial
    failure reported success and nobody could tell which company was missing.

    The ACTION is singular for a different reason: `A`, `U`, or both on one
    request, because `OPEN_BKDT` has no action parameter, so splitting it
    would write SAP rows identical in every column SAP reads.
    """

    #: THE COMPANIES the rights are granted in, as one canonical value —
    #: `OIL`, or `OIL,MART`, or `OIL,BEVERAGES,MART`. Canonical means
    #: deduplicated and in `COMPANY_CHOICES` order, so one set has one
    #: spelling; `core.companies.canonical_company_set` is the only writer.
    #:
    #: ONE REQUEST, ONE APPROVAL, ONE CALL PER COMPANY. The rights asked for
    #: are the same rights in each named database, so they are decided once:
    #: one flow, one workflow, one stage chain, one history. The SAP write is
    #: where the companies separate — `services/hana.apply_grant` makes one
    #: `OPEN_BKDT` call per company, into that company's own schema, and
    #: records each call's payload and each call's answer.
    #:
    #: That recording is what makes this safe. JSAP also accepted a
    #: comma-separated branch list, but its loop kept no per-branch result, so
    #: when one company landed and another failed there was no way to tell
    #: which — and the request claimed success. Here a partial failure is
    #: `hana_status = FAILED` with every company's outcome preserved in
    #: `hana_status_text`, because the requester does not have what they asked
    #: for until all of them do.
    #:
    #: NO `choices`, like `action`: a set is not one of the atoms, and Django
    #: would validate `OIL,MART` as invalid and render it unlabelled. The
    #: database CHECK below enumerates the legal sets instead.
    company = models.CharField(
        max_length=20, db_index=True,
        help_text='Companies whose SAP databases the rights are granted in, '
                  'canonical and comma-separated (e.g. "OIL,MART").',
    )
    #: The SAP login (`OUSR.USER_CODE`) the rights are for — NOT the OMS user
    #: raising the request. They are frequently different people.
    sap_username = models.CharField(
        max_length=20,
        help_text='SAP user the rights are granted to (OUSR.USER_CODE).',
    )
    #: THE document identity — the SAP object NAME, e.g. "G/L Accounts".
    #:
    #: The numeric `ObjType` column this replaced is gone. A name is what a
    #: requester picks, what an approver reads and what the history shows, and
    #: keeping a second numeric copy of the same fact is how the two come to
    #: disagree.
    #:
    #: SAP still needs the NUMBER — `OPEN_BKDT`'s `TRANSTYPE` is an INTEGER —
    #: so it is resolved from this name against `MOBJ` at the moment of the
    #: SAP call (`services.sap_masters.object_type_for`). That is safe because
    #: the mapping is one-to-one: `MOBJ` holds 75 objects with 75 distinct,
    #: non-blank names, identical across all three company schemas (verified
    #: against live HANA). A name SAP no longer knows fails the call loudly and
    #: recoverably rather than granting the wrong object — see §7.3.
    document_type_name = models.CharField(
        max_length=120,
        help_text='SAP object name (MOBJ.ObjName), e.g. "A/R Invoice".',
    )
    from_date = models.DateField(
        help_text='Start of the back-posting window (inclusive).')
    to_date = models.DateField(
        help_text='End of the back-posting window (inclusive).')
    #: When the granted rights expire. A real timestamp, because HANA's
    #: `TIMELIMIT` is a `TIMESTAMP` — not a duration, despite the name.
    #:
    #: REQUIRED, and that is not a policy choice. SAP's posting validator
    #: `SBO_SP_TRANSACTIONNOTIFICATION` contains 14 separate BKDT lookups and
    #: every one of them ends `AND CURRENT_TIMESTAMP < r."timeLimit"`. A NULL
    #: makes that comparison UNKNOWN, so the row never matches and the grant
    #: silently does nothing — an approved request that grants no rights. No
    #: row JSAP ever wrote has a NULL here (0 of 1,292 across the three live
    #: company schemas), so this matches the observed contract as well.
    time_limit = models.DateTimeField(
        help_text='When the granted rights lapse in SAP.',
    )
    #: `'A'`, `'U'`, or `'A,U'` for both — see `ACTION_VALUES`. Not a
    #: `choices` field, because the combined value is not one of the atoms.
    action = models.CharField(
        max_length=3, default=RequestAction.ADD,
    )

    # NO `remarks` COLUMN, deliberately. Remarks are EVENT-specific: the reason
    # for raising, the reason for an edit, an approver's note and a rejection
    # reason are four different statements by up to four different people. One
    # column could only hold the last of them, and overwriting a rejection
    # reason with a later edit's note is how an audit trail becomes untrue.
    # Every remark is a row in `BackDateActionLog` instead.

    #: ALWAYS the authenticated user. JSAP took this from the request body, so
    #: a request could be raised in somebody else's name.
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='backdate_requests',
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = _t('backdate')
        ordering = ['-created_at']
        verbose_name = 'BackDate'
        verbose_name_plural = 'BackDate requests'
        constraints = [
            # EVERY LEGAL SET, enumerated — seven of them for three
            # companies. A regex would also accept `MART,OIL` and `OIL,OIL`,
            # spellings the application never writes, so the database would be
            # agreeing to values that can only arrive through a bug.
            models.CheckConstraint(
                condition=Q(company__in=company_set_values()),
                name='backdate_company_valid',
            ),
            # The window must be a window. JSAP enforced this in the stored
            # procedure and nowhere in application code; stating it here means
            # it holds for every writer, including a data fix.
            models.CheckConstraint(
                condition=Q(to_date__gte=models.F('from_date')),
                name='backdate_dates_ordered',
            ),
            # The combined value is legal; an arbitrary string is not.
            models.CheckConstraint(
                condition=Q(action__in=list(ACTION_VALUES)),
                name='backdate_action_valid',
            ),
        ]
        indexes = [
            models.Index(fields=['created_by', '-created_at'],
                         name='bkdt_creator_idx'),
            models.Index(fields=['company'], name='bkdt_company_idx'),
        ]

    @property
    def companies(self):
        """The companies this request covers, in canonical order.

        THE FAN-OUT POINT. `services/hana.apply_grant` iterates this to make
        one `OPEN_BKDT` call per company, each into that company's own schema.
        Everything above it — the request, the flow, the workflow, the approval
        chain, the action log — stays singular.
        """
        return [c for c in self.company.split(COMPANY_SET_SEPARATOR) if c]

    @property
    def company_label(self):
        """`"OIL,MART"` -> `"Oil, Mart"`.

        Not `get_company_display()`: the column no longer declares `choices`,
        because a SET is not one of the atoms — the same reason `action` does
        not declare them for `A,U`. Django would return the raw value.
        """
        return company_set_labels(self.company)

    @property
    def action_label(self):
        return ACTION_LABELS.get(self.action, self.action)

    def __str__(self):
        return (f'BKDT#{self.pk} {self.company} {self.sap_username} '
                f'{self.from_date}..{self.to_date}')


class BackDateFlow(models.Model):
    """Where one request currently is, and what SAP said.

    ONE ROW PER REQUEST, and no task table underneath it. An earlier version
    opened a `backdate_approval_task` row per stage; every one of them held a
    stage id, a sequence and a status, which is precisely what this row already
    says about the stage that matters — the current one. The finished stages
    are in the action log, which is where history belongs. So the pending queue
    is a filter on this table:

        status = PENDING  AND  the caller is the current stage's effective user

    WHY `current_user` IS NOT THE AUTHORITY
    ---------------------------------------
    `current_stage` is. `current_user` is written alongside it so a list can be
    rendered and filtered without resolving every row through the engine, and
    it is refreshed whenever this module resolves the stage — but the question
    "who may act now?" is always answered by
    `workflow.services.assignments.get_stage_assignment(current_stage_id)`,
    which applies `workflow_user_replacements` for today's date.

    That is what makes an administrator's stage-user change apply instantly to
    requests already waiting, with nothing migrated (plan §44), and what makes
    a temporary replacement open and close on its dates with no write here
    (§45). A stored user consulted as the authority would freeze both.
    """

    backdate = models.OneToOneField(
        BackDate, on_delete=models.CASCADE, related_name='flow',
        db_column='backdate_id',
    )
    status = models.CharField(
        max_length=10, choices=FlowStatus.choices, default=FlowStatus.PENDING,
        db_index=True,
    )
    #: NULL until the final approval writes to SAP. See `HanaStatus`.
    #:
    #: One value for the whole request even when it produced several SAP calls:
    #: SUCCESS only if every call succeeded. There is no PARTIAL, because a
    #: requester asked for rights and either has all of them or does not — the
    #: per-company detail is in `hana_status_text`, which says exactly which
    #: call failed and why.
    hana_status = models.CharField(
        max_length=10, choices=HanaStatus.choices, null=True, blank=True,
    )
    #: The exact parameters sent to `OPEN_BKDT`, one entry per company.
    #:
    #: Stored because one request can produce several SAP calls that disagree —
    #: OIL succeeds, BEVERAGES fails — and because what was sent is not
    #: reproducible from the request afterwards: an edit, a renamed stage or a
    #: changed setting all move the inputs. What was ACTUALLY sent is the only
    #: thing worth keeping.
    #:
    #: Business parameters only. No connection string, no credentials, no
    #: resolved schema name: those describe how the call was made, not what was
    #: asked for, and an audit column is the wrong place for them.
    sap_payload = models.JSONField(null=True, blank=True, default=None)
    #: What SAP actually said, per call, serialised as JSON — never replaced by
    #: a generic application message. `OPEN_BKDT` returns no result set, so a
    #: success is the driver's own confirmation and a failure is the database
    #: error verbatim.
    hana_status_text = models.TextField(blank=True, default='')

    #: Denormalised from `current_stage`. See the class docstring.
    current_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    #: The workflow the engine selected at submission. PROTECT, because
    #: deleting configuration that explains a past decision would silently
    #: rewrite history.
    #:
    #: The matched QUERY is not stored. It explains the selection at the
    #: instant it happened and nothing reads it afterwards; the workflow is the
    #: fact that keeps mattering.
    workflow = models.ForeignKey(
        'workflow.Workflow', on_delete=models.PROTECT, related_name='+',
    )
    #: The engine stage awaiting a decision; NULL once the flow is finished.
    current_stage = models.ForeignKey(
        'workflow.WorkflowStage', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
        db_column='current_stage',
    )
    #: How many stages the chosen workflow had AT SUBMISSION — so "stage 2 of
    #: 3" still reads correctly after a stage is added or retired. It is a
    #: count, never a name: names live on `workflow_stages`.
    total_stage = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    #: Also the closest thing to "when SAP was written", because that write is
    #: the last thing that touches an approved flow. It is NOT a dedicated
    #: immutable HANA timestamp — any later change to this row moves it.
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = _t('backdate_flow')
        ordering = ['-created_at']
        verbose_name = 'BackDate Flow'
        indexes = [
            # The approval queue: pending work for one user.
            models.Index(fields=['status', 'current_user'],
                         name='bkdt_flow_queue_idx'),
            # "is anything still sitting on this stage?" — asked whenever a
            # stage's user is changed or a replacement is configured.
            models.Index(fields=['current_stage'],
                         name='bkdt_flow_stage_idx'),
            models.Index(fields=['workflow'], name='bkdt_flow_workflow_idx'),
        ]

    def __str__(self):
        return f'flow#{self.pk} bkdt#{self.backdate_id} {self.status}'


class BackDateActionLog(models.Model):
    """Append-only history. Nothing in this module ever updates or deletes one.

    WHAT IS NOT HERE, AND WHY
    -------------------------
    * `sequence` — `acted_at` and `id` already order the rows, and a hand-kept
      counter is one more thing that can disagree with them.
    * `stage_name` — resolved from `stage` for display. A log row that stored
      the name would keep showing a stage's OLD name after a rename, which
      reads as though a different stage acted.
    * `on_behalf_of` — the actor is `acted_by`. Who was *configured* at the
      time is the replacement table's business, and duplicating it here gave
      two actor columns that could disagree.
    * `flow` — the log belongs to the REQUEST. A flow is one-per-request, so
      pointing at the flow only added a hop.
    """

    backdate = models.ForeignKey(
        BackDate, on_delete=models.CASCADE, related_name='action_logs',
        db_column='backdate_id',
    )
    action = models.CharField(max_length=10, choices=LogAction.choices)
    acted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='backdate_action_logs',
    )
    #: The stage being decided. NULL for CREATE and UPDATE, which happen
    #: before/outside stage execution.
    stage = models.ForeignKey(
        'workflow.WorkflowStage', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+', db_column='stage_id',
    )
    remarks = models.TextField(blank=True, default='')
    #: WHAT an edit changed: `{field: {'old': ..., 'new': ...}}` for the
    #: CHANGED fields only, following `payments.PaymentStatusHistory`.
    #:
    #: Written for UPDATE rows and nothing else: every other action is a
    #: transition, not a field change, and a snapshot on those would turn this
    #: table into an event store. NULL when nothing tracked changed, so a row
    #: never claims an edit it cannot describe.
    action_data = models.JSONField(null=True, blank=True, default=None)
    acted_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = _t('backdate_action_logs')
        ordering = ['acted_at', 'id']
        verbose_name = 'BackDate Action Log'
        indexes = [
            models.Index(fields=['backdate', 'acted_at'],
                         name='bkdt_log_backdate_idx'),
        ]

    def __str__(self):
        return f'{self.action} bkdt#{self.backdate_id} #{self.pk}'
