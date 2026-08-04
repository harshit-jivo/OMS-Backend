"""Generic, multi-level approval engine.

Ported from `tracker` (Stage / UserStageAccess / StageEvent) and generalised
with a GenericForeignKey so one engine serves Payment, Deposit and — later —
orders.Order.

Why not extend the `orders` flow: `OrderRateApproval.order` is a concrete FK to
Order (orders/models.py:435-439), and `OrderFlowConfig` expresses stages as
three independent booleans with no sequence column (orders/models.py:211-213),
so "Level 2 of 3" cannot be represented there at all.
"""
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.db.models import Q

from core.models import TimeStampedModel

# Mirrors sap_sync.Party.category — in this system the "company" IS the
# category, and it maps 1:1 to a SAP company DB.
CATEGORY_CHOICES = [
    ('OIL', 'Oil'),
    ('BEVERAGES', 'Beverages'),
    ('MART', 'Mart'),
]


class DocumentType(models.TextChoices):
    PAYMENT = 'PAYMENT', 'Payment Receipt'
    DEPOSIT = 'DEPOSIT', 'Bank Deposit'
    ORDER = 'ORDER', 'Sales Order'


class ApprovalWorkflow(TimeStampedModel):
    """A named ladder of levels for one document type (optionally per company)."""

    code = models.CharField(max_length=50, unique=True)          # 'PAYMENT_OIL_V1'
    name = models.CharField(max_length=100)
    document_type = models.CharField(
        max_length=30, choices=DocumentType.choices, db_index=True)
    # Blank = applies to every company.
    company = models.CharField(
        max_length=20, choices=CATEGORY_CHOICES, blank=True, default='')
    # A rejected document restarts from level 1 on resubmit. Kept as a flag so
    # the alternative (resume at the rejecting level) is a config change later.
    restart_on_reject = models.BooleanField(default=True)
    # Segregation of duties: the submitter may never approve their own document.
    forbid_self_approval = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'approval_workflow'
        ordering = ['document_type', 'company']
        constraints = [
            # Partial unique: only ONE active workflow per (type, company).
            # Superseded workflows must survive because historical requests FK
            # into their levels, so uniqueness can only apply to active rows —
            # which unique_together cannot express.
            models.UniqueConstraint(
                fields=['document_type', 'company'],
                condition=Q(is_active=True),
                name='approval_workflow_one_active_per_type_company',
            ),
        ]

    def __str__(self):
        return f'{self.code} ({self.get_document_type_display()})'


class ApprovalLevel(models.Model):
    """One rung. `sequence` is the ordering column OrderFlowConfig lacks."""

    workflow = models.ForeignKey(
        ApprovalWorkflow, on_delete=models.CASCADE, related_name='levels')
    sequence = models.PositiveSmallIntegerField()                # 1, 2, 3 ...
    name = models.CharField(max_length=60)                       # 'Accountant Review'
    role = models.ForeignKey(
        'users.UserRole', on_delete=models.PROTECT,
        null=True, blank=True, related_name='approval_levels')
    # How many distinct approvals clear this rung. Always 1 — one approver per
    # stage. Kept as a field (rather than dropped) because the quorum check in
    # services.approve reads it; editable only from Django admin if a
    # multi-approver stage is ever genuinely needed.
    min_approvals = models.PositiveSmallIntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'approval_level'
        ordering = ['workflow_id', 'sequence']
        constraints = [
            # Scoped per workflow, NOT globally unique. tracker.Stage.order is
            # globally unique (tracker/models.py:105), which is fine for one
            # hardcoded flow but breaks the moment a second workflow exists.
            models.UniqueConstraint(
                fields=['workflow', 'sequence'], name='approval_level_seq_uq'),
        ]

    def __str__(self):
        return f'L{self.sequence} {self.name}'


class ApprovalLevelApprover(models.Model):
    """Explicit user grant at a level — the port of tracker.UserStageAccess.

    Additive to the level's role: a user qualifies via role OR an explicit grant.
    """

    level = models.ForeignKey(
        ApprovalLevel, on_delete=models.CASCADE, related_name='approvers')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='approval_level_grants')
    # Blank = the grant applies in every company.
    company = models.CharField(
        max_length=20, choices=CATEGORY_CHOICES, blank=True, default='')
    is_active = models.BooleanField(default=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='approval_grants_made')
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'approval_level_approver'
        constraints = [
            models.UniqueConstraint(
                fields=['level', 'user', 'company'],
                name='approval_level_approver_uq'),
        ]
        indexes = [
            models.Index(fields=['user', 'is_active'], name='idx_ala_user_active'),
            models.Index(fields=['level', 'is_active'], name='idx_ala_level_active'),
        ]

    def __str__(self):
        return f'{self.user_id} @ {self.level_id}'


class ApprovalRequest(TimeStampedModel):
    """The live approval state of ONE document, attached generically."""

    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        PENDING = 'PENDING', 'Pending approval'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'
        CANCELLED = 'CANCELLED', 'Cancelled'

    workflow = models.ForeignKey(
        ApprovalWorkflow, on_delete=models.PROTECT, related_name='requests')

    content_type = models.ForeignKey(ContentType, on_delete=models.PROTECT)
    object_id = models.PositiveBigIntegerField()
    document = GenericForeignKey('content_type', 'object_id')

    # Denormalised at submit so the approver inbox can filter and sort without
    # joining to the polymorphic target (impossible in one SQL query).
    company = models.CharField(max_length=20, choices=CATEGORY_CHOICES, db_index=True)
    amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    document_number = models.CharField(max_length=50, blank=True, default='')

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True)
    current_level = models.PositiveSmallIntegerField(default=1)
    # Snapshotted at submit. If an admin edits the workflow mid-flight, an
    # in-progress request keeps the ladder it started on — otherwise
    # "Level 2 of 3" could silently become "Level 2 of 2" and skip an approval.
    total_levels = models.PositiveSmallIntegerField(default=0)
    # Incremented on every resubmit so prior actions stay in the log but are
    # clearly attributed to an earlier attempt.
    round_number = models.PositiveSmallIntegerField(default=1)

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        null=True, blank=True, related_name='approval_requests_submitted')
    submitted_at = models.DateTimeField(null=True, blank=True)
    level_entered_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'approval_request'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'current_level'], name='idx_apreq_queue'),
            models.Index(fields=['content_type', 'object_id'], name='idx_apreq_target'),
            models.Index(fields=['company', 'status', '-created_at'],
                         name='idx_apreq_list'),
        ]
        constraints = [
            # Structurally prevents two open approval chains on one document —
            # the classic double-submit bug.
            models.UniqueConstraint(
                fields=['content_type', 'object_id'],
                condition=Q(status__in=['DRAFT', 'PENDING']),
                name='approval_request_one_open_per_document',
            ),
        ]

    def __str__(self):
        return f'{self.document_number or self.pk} [{self.status}]'

    @property
    def level_label(self):
        """'Level 2 of 3' — the thing the orders flow cannot express."""
        return f'Level {self.current_level} of {self.total_levels}'


class ApprovalAction(models.Model):
    """Append-only decision log.

    NEVER updated after insert. `orders_log` is mutated in place
    (orders/views.py:3350-3372), which destroys prior remarks, and it has no
    `updated_at` — so the actual decision timestamp is unrecoverable there.
    """

    class Action(models.TextChoices):
        SUBMIT = 'SUBMIT', 'Submitted'
        APPROVE = 'APPROVE', 'Approved'
        REJECT = 'REJECT', 'Rejected'
        CANCEL = 'CANCEL', 'Cancelled'
        RESUBMIT = 'RESUBMIT', 'Resubmitted'

    request = models.ForeignKey(
        ApprovalRequest, on_delete=models.CASCADE, related_name='actions')
    sequence = models.PositiveIntegerField()          # monotonic per request
    round_number = models.PositiveSmallIntegerField(default=1)
    level = models.PositiveSmallIntegerField(default=0)
    level_name = models.CharField(max_length=60, blank=True, default='')

    action = models.CharField(max_length=12, choices=Action.choices)
    remarks = models.TextField(blank=True, default='')

    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='approval_actions')
    # Snapshots so the log survives a rename/deactivation, matching
    # audit.AuditLog.username (audit/models.py:20).
    approver_username = models.CharField(max_length=150, blank=True, default='')
    approver_role = models.CharField(max_length=50, blank=True, default='')

    # The audit app captures neither of these (audit/models.py:5-30). For money
    # movement both are required.
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=400, blank=True, default='')

    acted_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'approval_action'
        ordering = ['request_id', 'sequence']
        constraints = [
            models.UniqueConstraint(
                fields=['request', 'sequence'], name='approval_action_seq_uq'),
        ]
        indexes = [
            models.Index(fields=['approver', '-acted_at'], name='idx_apact_user'),
        ]

    def __str__(self):
        return f'{self.action} L{self.level} by {self.approver_username}'
