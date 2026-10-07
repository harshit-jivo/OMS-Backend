"""Credit Limit (CREDIT_LIMIT) — raising a customer's credit limit in SAP.

WHAT THIS MODULE IS
-------------------
Someone asks for a customer's credit limit to be raised. The request is
approved through a configured chain, and the final approval writes the new
limit onto the business partner in SAP (`OCRD.CreditLine`, through the Service
Layer). It replaces JSAP's `cl` schema, which OMS no longer talks to.

OWNERSHIP
---------
The Workflow Engine answers "which workflow applies, and who approves at each
stage?". Everything after that is this module's, in its own schema:

    CreditLimitRequest     the business document
    CreditLimitFlow        where it currently is, and what SAP said
    CreditLimitActionLog   what was decided, by whom, when

Same shape as `backdate` and `production`; see `docs/architecture/
WORKFLOW_MODULE_INTEGRATION.md`.

THE CUSTOMER FACTS ARE SAP'S, READ AT SUBMISSION
------------------------------------------------
Name, main group, balance and current limit are read from OCRD on the server
when the request is raised, never taken from the client. They are what the
approver decides against, and what the workflow queries match on — JSAP took
them from the request body, which is how its OMS-raised requests came to carry
a customer NAME where the queries expected the group.

MIGRATION NOTE
--------------
A fresh module. No JSAP credit documents are imported and no table here
carries a JSAP id. JSAP keeps its own history for reference.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q

from core.companies import COMPANY_CHOICES, COMPANY_CODES


def _t(table):
    """Schema-qualify a table for the dedicated `credit_limit` schema."""
    return f'credit_limit"."{table}'


class FlowStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    #: Final. The new limit IS in SAP — the write happens before this is set.
    APPROVED = 'APPROVED', 'Approved'
    #: Final. One rejection ends the request; a new one must be raised.
    REJECTED = 'REJECTED', 'Rejected'


class LogAction(models.TextChoices):
    CREATE = 'CREATE', 'Created'
    APPROVE = 'APPROVE', 'Approved'
    REJECT = 'REJECT', 'Rejected'


def _attachment_path(instance, filename):
    return f'credit_limit/{instance.company}/{instance.card_code}/{filename}'


class CreditLimitRequest(models.Model):
    """One request to set a customer's credit limit in one company."""

    company = models.CharField(max_length=20, choices=COMPANY_CHOICES,
                               db_index=True)
    card_code = models.CharField(max_length=50, db_index=True)

    #: OCRD facts at submission — see the module docstring. Snapshots, on
    #: purpose: the approver must see what the requester saw.
    card_name = models.CharField(max_length=200, blank=True, default='')
    #: `OCRD.U_Main_Group` — GT, MT, STAFF, CORPORATE... What JSAP called
    #: `customerValue`, and what the workflow queries route on.
    main_group = models.CharField(max_length=100, blank=True, default='')
    current_balance = models.DecimalField(max_digits=19, decimal_places=2)
    current_credit_limit = models.DecimalField(max_digits=19, decimal_places=2)

    new_credit_limit = models.DecimalField(max_digits=19, decimal_places=2)
    #: Recorded, not enforced. JSAP stored it too and SAP never reverted a
    #: limit when it passed — verified against OCRD history — so neither does
    #: this module. It says how long the requester expects to need the limit.
    valid_till = models.DateField()
    remarks = models.TextField(blank=True, default='')
    #: Optional supporting document.
    attachment = models.FileField(upload_to=_attachment_path, max_length=500,
                                  blank=True, default='')

    #: Set when the request was raised from an invoice SAP refused on credit.
    #: One request per invoice.
    invoice_log = models.OneToOneField(
        'invoice.InvoiceLog', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='credit_limit_request',
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='credit_limit_requests',
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = _t('credit_limit_request')
        ordering = ['-created_at']
        verbose_name = 'Credit Limit Request'
        constraints = [
            models.CheckConstraint(condition=Q(company__in=COMPANY_CODES),
                                   name='cl_request_company_valid'),
            models.CheckConstraint(condition=Q(new_credit_limit__gt=0),
                                   name='cl_request_new_limit_positive'),
        ]

    def __str__(self):
        return f'CL#{self.pk} {self.company} {self.card_code}'


class CreditLimitFlow(models.Model):
    """Where one request is, and what SAP said. One row per request.

    `current_stage` is the authority for who acts — resolved through the
    engine on every read. `current_user` is a denormalised copy for display.
    See `backdate.models.BackDateFlow`, which this mirrors.
    """

    request = models.OneToOneField(
        CreditLimitRequest, on_delete=models.CASCADE, related_name='flow',
    )
    status = models.CharField(max_length=10, choices=FlowStatus.choices,
                              default=FlowStatus.PENDING, db_index=True)
    workflow = models.ForeignKey(
        'workflow.Workflow', on_delete=models.PROTECT, related_name='+',
    )
    current_stage = models.ForeignKey(
        'workflow.WorkflowStage', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    current_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    #: Stage count at submission, so "2 of 3" reads right after a config edit.
    total_stage = models.PositiveSmallIntegerField(default=0)

    #: What the final approval sent to SAP, and what SAP answered. On a
    #: refusal these are written after the rollback (see `services.sap`), so
    #: the approver can read why while the request stays PENDING.
    sap_payload = models.JSONField(null=True, blank=True, default=None)
    sap_response = models.TextField(blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = _t('credit_limit_flow')
        ordering = ['-created_at']
        verbose_name = 'Credit Limit Flow'
        indexes = [
            models.Index(fields=['status', 'current_stage'],
                         name='cl_flow_queue_idx'),
        ]

    def __str__(self):
        return f'flow#{self.pk} CL#{self.request_id} {self.status}'


class CreditLimitActionLog(models.Model):
    """Append-only history. Never updated or deleted by this module."""

    request = models.ForeignKey(
        CreditLimitRequest, on_delete=models.CASCADE, related_name='action_logs',
    )
    action = models.CharField(max_length=10, choices=LogAction.choices)
    acted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    #: The stage decided. NULL for CREATE.
    stage = models.ForeignKey(
        'workflow.WorkflowStage', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    remarks = models.TextField(blank=True, default='')
    acted_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = _t('credit_limit_action_log')
        ordering = ['acted_at', 'id']
        verbose_name = 'Credit Limit Action Log'

    def __str__(self):
        return f'{self.action} CL#{self.request_id} #{self.pk}'
