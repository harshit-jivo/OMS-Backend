"""BUDGET — budget approval of SAP drafts.

WHAT THIS MODULE IS
-------------------
Someone saves an expense document in SAP (an A/P invoice, an expense payment,
a journal voucher…). SAP keeps it as a DRAFT and refuses to post it until every
gated line has an approval row in the table its gate reads. OMS notices the
draft, routes each line's budget head to its owner, and writes the decision to
that table — `OMS_BUDGET_APPROVALS`, which SAP reads through the view
`tbl_Draft_Approvals` (`hana/management/commands/budget_gate.py`).

OMS never creates, edits or posts a document. It notices, decides, tells SAP.

THE APPROVAL UNIT IS THE ITEM
-----------------------------
SAP gates LINES. One draft can carry lines from two budget heads — a Factory
bill with an electricity line — and each head has its own approver. So:

    BudgetDraft       one SAP draft, snapshotted
    BudgetLine        one gated line of it (what SAP's gate looks up)
    BudgetItem        the lines of one draft that share a route: what ONE
                      approval chain decides. Carries its own flow.
    BudgetActionLog   append-only history, per item

A draft is APPROVED only when every one of its items is.

See `docs/Approvals/BUDGET_DESIGN.md` for the decisions and
`docs/Approvals/BUDGET_JSAP_SAP.md` for how JSAP did it.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q

from core.companies import COMPANY_CHOICES, COMPANY_CODES


def _t(table):
    """Schema-qualify a table for the dedicated `budget` Postgres schema."""
    return f'budget"."{table}'


class ObjType(models.IntegerChoices):
    """SAP object types that reach budget approval (`DRAFT_APPROVAL`)."""

    AR_CREDIT_MEMO = 14, 'A/R credit memo'
    DELIVERY = 15, 'Delivery'
    AP_INVOICE = 18, 'A/P invoice'
    AP_CREDIT_MEMO = 19, 'A/P credit memo'
    JOURNAL_VOUCHER = 28, 'Journal voucher'
    OUTGOING_PAYMENT = 46, 'Outgoing payment'
    GOODS_RECEIPT = 59, 'Goods receipt'
    GOODS_ISSUE = 60, 'Goods issue'


class DraftStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    APPROVED = 'APPROVED', 'Approved'
    REJECTED = 'REJECTED', 'Rejected'
    #: Posted or deleted in SAP before it was decided: the question stopped
    #: being asked. Not a decision and not a failure.
    GONE = 'GONE', 'No longer waiting in SAP'


class ItemStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    APPROVED = 'APPROVED', 'Approved'
    #: Final for this version of the draft. A draft changed in SAP comes back
    #: as new items (the old ones become SUPERSEDED).
    REJECTED = 'REJECTED', 'Rejected'
    GONE = 'GONE', 'No longer waiting in SAP'
    #: The draft was changed in SAP after this item was raised; what was
    #: approved (or not) no longer describes it. Replaced by fresh items.
    SUPERSEDED = 'SUPERSEDED', 'Replaced — the draft changed in SAP'


class SapWriteStatus(models.TextChoices):
    SUCCESS = 'SUCCESS', 'Written to SAP'
    FAILED = 'FAILED', 'SAP write failed'


class LogAction(models.TextChoices):
    #: The sync raised the item. `acted_by` is NULL: no OMS user did this.
    SYNC = 'SYNC', 'Taken in from SAP'
    APPROVE = 'APPROVE', 'Approved'
    #: The 48-hour rule approved it on the stage user's behalf.
    AUTO_APPROVE = 'AUTO_APPROVE', 'Auto-approved'
    REJECT = 'REJECT', 'Rejected'
    GONE = 'GONE', 'No longer waiting in SAP'
    SUPERSEDED = 'SUPERSEDED', 'The draft changed in SAP'


class BudgetDraft(models.Model):
    """One SAP draft waiting for SAP's approval, snapshotted when OMS saw it.

    Keyed `(company, obj_type, draft_entry)`: draft numbers run per company
    and per draft table — a journal voucher's batch number and an A/P draft's
    DocEntry are different sequences.
    """

    company = models.CharField(max_length=20, choices=COMPANY_CHOICES, db_index=True)
    obj_type = models.PositiveSmallIntegerField(choices=ObjType.choices)
    #: The draft's key as SAP's gate looks it up: ODRF / OPDF DocEntry, or the
    #: journal voucher's BatchNum.
    draft_entry = models.IntegerField()
    doc_num = models.IntegerField(null=True, blank=True)
    doc_date = models.DateField(null=True, blank=True)
    card_code = models.CharField(max_length=50, blank=True, default='')
    card_name = models.CharField(max_length=200, blank=True, default='')
    #: Who raised it in SAP (OUSR.U_NAME). Text, never an FK: a SAP login is a
    #: different identity namespace from an OMS user.
    sap_created_by = models.CharField(max_length=100, blank=True, default='')
    comments = models.TextField(blank=True, default='')
    #: Hash of the gated lines (line, account, budget, amount). A different
    #: value on a later sync means the draft was changed in SAP.
    fingerprint = models.CharField(max_length=64, blank=True, default='')
    status = models.CharField(max_length=10, choices=DraftStatus.choices,
                              default=DraftStatus.PENDING, db_index=True)
    synced_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = _t('budget_draft')
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(fields=['company', 'obj_type', 'draft_entry'], name='budget_draft_sap_uq'),
            models.CheckConstraint(condition=Q(company__in=COMPANY_CODES), name='budget_draft_company_valid'),
        ]
        indexes = [models.Index(fields=['company', 'status'], name='budget_draft_co_status_idx')]

    def __str__(self):
        return f'{self.company} {self.get_obj_type_display()} draft {self.draft_entry}'


class BudgetItem(models.Model):
    """The lines of one draft that share a route, and the flow deciding them.

    One row per (draft, route), flow included — no separate flow table, as in
    BKDT / PRDO: the stage it waits at is here, passed stages are in the log,
    stages ahead are the engine's configuration.

    `company` and `route` are what the BUDGET workflow queries read:

        SELECT id FROM "budget"."budget_item" WHERE company = 'OIL' AND route = 'FACTORY'
    """

    draft = models.ForeignKey(BudgetDraft, on_delete=models.CASCADE, related_name='items')
    company = models.CharField(max_length=20, choices=COMPANY_CHOICES, db_index=True)
    #: `<HEAD>` or `<HEAD>_ELECTRICITY` (`services.routing.route_for`).
    route = models.CharField(max_length=60, db_index=True)
    budget_code = models.CharField(max_length=50)
    amount = models.DecimalField(max_digits=19, decimal_places=2)
    status = models.CharField(max_length=10, choices=ItemStatus.choices,
                              default=ItemStatus.PENDING, db_index=True)

    workflow = models.ForeignKey('workflow.Workflow', on_delete=models.PROTECT,
                                 null=True, blank=True, related_name='+')
    current_stage = models.ForeignKey('workflow.WorkflowStage', on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name='+')
    #: Denormalised for lists; the authority is `get_stage_assignment`.
    current_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name='+')
    total_stage = models.PositiveSmallIntegerField(default=0)
    #: When it reached its current stage — what the auto-approval measures.
    waiting_since = models.DateTimeField(null=True, blank=True)
    #: Bumped on every move: a decision made on a stale screen is refused.
    version = models.PositiveIntegerField(default=0)

    sap_status = models.CharField(max_length=10, choices=SapWriteStatus.choices, null=True, blank=True)
    sap_status_text = models.TextField(blank=True, default='')
    sap_written_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = _t('budget_item')
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(condition=Q(company__in=COMPANY_CODES), name='budget_item_company_valid'),
        ]
        indexes = [
            models.Index(fields=['status', 'current_stage'], name='budget_item_queue_idx'),
            models.Index(fields=['company', 'route'], name='budget_item_route_idx'),
        ]

    def __str__(self):
        return f'{self.draft} / {self.route} [{self.status}]'

    @property
    def is_open(self):
        return self.status == ItemStatus.PENDING


class BudgetLine(models.Model):
    """One gated line: what SAP's gate looks up, `(ObjType, DocEntry, LineNum)`."""

    draft = models.ForeignKey(BudgetDraft, on_delete=models.CASCADE, related_name='lines')
    item = models.ForeignKey(BudgetItem, on_delete=models.CASCADE, related_name='lines')
    #: DRF1.LineNum, PDF4.LineId or BTF1.Line_ID — as the gate reads it.
    line_num = models.IntegerField()
    vis_order = models.IntegerField(null=True, blank=True)
    acct_code = models.CharField(max_length=50)
    acct_name = models.CharField(max_length=200, blank=True, default='')
    budget_code = models.CharField(max_length=50)
    sub_budget_code = models.CharField(max_length=50, blank=True, default='')
    #: OcrCode2 — the month the expense belongs to, as SAP tags it.
    effect_month = models.CharField(max_length=50, blank=True, default='')
    amount = models.DecimalField(max_digits=19, decimal_places=2)
    remarks = models.TextField(blank=True, default='')

    class Meta:
        db_table = _t('budget_line')
        ordering = ['draft', 'line_num']
        constraints = [
            models.UniqueConstraint(fields=['item', 'line_num'], name='budget_line_item_uq'),
        ]


class BudgetActionLog(models.Model):
    """Append-only. Transitions only — never a row for a poll that changed nothing."""

    item = models.ForeignKey(BudgetItem, on_delete=models.CASCADE, related_name='action_logs')
    action = models.CharField(max_length=15, choices=LogAction.choices)
    #: NULL for SYNC, GONE, SUPERSEDED and AUTO_APPROVE: no OMS user did those.
    acted_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                 null=True, blank=True, related_name='+')
    stage = models.ForeignKey('workflow.WorkflowStage', on_delete=models.SET_NULL,
                              null=True, blank=True, related_name='+')
    remarks = models.TextField(blank=True, default='')
    action_data = models.JSONField(null=True, blank=True, default=None)
    acted_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = _t('budget_action_log')
        ordering = ['acted_at', 'id']
        indexes = [models.Index(fields=['item', 'acted_at'], name='budget_log_item_idx')]


class BudgetSettings(models.Model):
    """The auto-approval setting. One row (`load()`)."""

    #: Off until someone turns it on: JSAP's version had been silently dead
    #: since March 2026, so nobody is relying on it today.
    auto_approve_enabled = models.BooleanField(default=False)
    auto_approve_hours = models.PositiveSmallIntegerField(default=48)
    #: People the auto-approval never acts for. JSAP hard-coded three user ids
    #: inside the procedure; here it is configuration.
    exempt_users = models.ManyToManyField(settings.AUTH_USER_MODEL, blank=True, related_name='+')
    #: {company: ISO time} of the last sync that read SAP successfully — so
    #: "is the feed alive?" is answerable from the data. JSAP's production feed
    #: died for 7 weeks while its job reported success every minute.
    last_sync = models.JSONField(default=dict, blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = _t('budget_settings')

    @classmethod
    def load(cls):
        row, _created = cls.objects.get_or_create(pk=1)
        return row
