"""Advance Payment's own tables.

`Employee` is the employee master, with each person's ROLE in the org
hierarchy: HOD, Sub-HOD or Executive. It was first loaded from JSAP's
employee hierarchy by migration 0002, from `data/jsap_employees.psv`.

Nothing is ever hard-deleted: `is_deleted` / `deleted_on` / `deleted_by`
mark a row gone, and `Employee.objects.alive()` leaves those out.
"""
from django.conf import settings
from django.db import models
from django.db.models import F, Q
from django.utils import timezone

from core.companies import COMPANY_CHOICES


class EmployeeRole(models.IntegerChoices):
    """JSAP's role types (`GetRoleTypes`), with JSAP's own ids."""

    HOD = 1, 'HOD'
    SUB_HOD = 2, 'Sub-HOD'
    EXECUTIVE = 3, 'Executive'


class Gender(models.TextChoices):
    MALE = 'M', 'Male'
    FEMALE = 'F', 'Female'


class EmployeeQuerySet(models.QuerySet):
    def alive(self):
        """Not deleted. (Active or not: an inactive employee still exists.)"""
        return self.filter(is_deleted=False)

    def active(self):
        """Not deleted, and currently active."""
        return self.filter(is_deleted=False, is_active=True)


class Employee(models.Model):
    #: JSAP's EmployeeId. Empty for an employee added in OMS itself.
    employee_id = models.PositiveIntegerField(
        unique=True, null=True, blank=True,
        help_text="JSAP's EmployeeId; empty for an employee added in OMS.")
    #: e.g. JWPL0115, or TEMP0001 for someone without a code yet. Stored upper-case.
    employee_code = models.CharField(max_length=20, unique=True)
    employee_name = models.CharField(max_length=150)
    email = models.EmailField(max_length=254, blank=True, null=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    designation = models.CharField(max_length=100, blank=True, null=True)

    role = models.PositiveSmallIntegerField(
        choices=EmployeeRole.choices, default=EmployeeRole.EXECUTIVE)
    gender = models.CharField(max_length=1, choices=Gender.choices, blank=True, null=True)

    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False)
    deleted_on = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+')
    #: When the row was added to OMS (the load date, for rows from JSAP).
    created_on = models.DateTimeField(default=timezone.now)

    objects = EmployeeQuerySet.as_manager()

    class Meta:
        db_table = 'advance_payment_employee'
        ordering = ['employee_name']
        indexes = [
            models.Index(fields=['role'], name='ap_employee_role_idx'),
            models.Index(fields=['is_active', 'is_deleted'], name='ap_employee_state_idx'),
        ]

    def __str__(self):
        return f'{self.employee_code} {self.employee_name}'

    def save(self, *args, **kwargs):
        self.employee_code = (self.employee_code or '').strip().upper()
        super().save(*args, **kwargs)

    def soft_delete(self, by=None):
        self.is_deleted = True
        self.is_active = False
        self.deleted_on = timezone.now()
        self.deleted_by = by
        self.save(update_fields=['is_deleted', 'is_active', 'deleted_on', 'deleted_by'])


class Department(models.Model):
    """A department, as JSAP held it, now OMS's own list.

    Copied from JSAP (`GetDepartments`) on 2026-09-24 because JSAP is being
    shut down; from then on OMS is the master. The ids are JSAP's, kept as
    they were, so a workflow query's `department_id = 35` means Finance in
    both. Departments added in OMS continue after the highest copied id.
    """

    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)
    created_on = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'advance_payment_department'
        ordering = ['name']

    def __str__(self):
        return self.name


class SubDepartment(models.Model):
    """A sub-department of a department, copied from JSAP with its id.

    Names repeat across departments ("Media" is under three), so a
    sub-department is only ever meaningful with its department.
    """

    id = models.AutoField(primary_key=True)
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name='sub_departments')
    name = models.CharField(max_length=150)
    is_active = models.BooleanField(default=True)
    created_on = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'advance_payment_sub_department'
        ordering = ['department__name', 'name']
        constraints = [
            models.UniqueConstraint(fields=['department', 'name'], name='ap_sub_department_name_uq'),
        ]

    def __str__(self):
        return f'{self.department.name} / {self.name}'


# ===========================================================================
# Payment requests and their approval
# ===========================================================================
#
#   AdvanceRequest ─┬─ RequestDocument   the bills / POs it pays (a snapshot)
#                   ├─ RequestFile       every file uploaded to it
#                   ├─ Payout ── PayoutLine   how it is paid (Payment stage)
#                   ├─ RequestFlow       where it is in its workflow (1 row)
#                   ├─ SapVoucher        each voucher posted / cancelled
#                   └─ RequestLog        its whole lifecycle, append-only
#
# The APPROVAL ROUTE is not stored here: it is the Workflow Engine's
# configuration (module ADVANCE_PAYMENT), chosen by the engine's queries
# against this request's company / department / sub-department. The flow row
# points at the stage it waits at, and "who may act now?" is always answered
# by `workflow.services.assignments.get_stage_assignment(stage)`, so changing
# a stage's user in the Workflows page moves pending requests with it.
#
# ONE USER PER STAGE (the engine's rule), so there is no task table: the stage
# waited at is on the flow row, the stages passed are in the log, the stages
# ahead are the engine's configuration. Same shape as PRDO.
#
# Any number of approval stages, named freely, then three fixed ones told
# apart by NAME: Payment, Audit, Final (see StageRole).


class RequestType(models.TextChoices):
    VENDOR = 'VENDOR', 'Vendor'
    EMPLOYEE_ADVANCE = 'EMPLOYEE_ADVANCE', 'Employee'
    EMPLOYEE_IMPREST = 'EMPLOYEE_IMPREST', 'Employee Imprest'


class PaymentAgainst(models.TextChoices):
    ADVANCE = 'ADVANCE', 'Advance'
    AGAINST_BILL = 'AGAINST_BILL', 'Against Bill'
    AGAINST_PO = 'AGAINST_PO', 'Against PO'
    ALL = 'ALL', 'All'
    #: A typed answer; the text is in `payment_against_other`.
    OTHER = 'OTHER', 'Other'


class ReturnMethod(models.TextChoices):
    ONE_TIME = 'ONE_TIME', 'One Time'
    EMI = 'EMI', 'EMI'
    CUSTOM = 'CUSTOM', 'Other / Custom'


class Priority(models.TextChoices):
    LOW = 'LOW', 'Low'
    MEDIUM = 'MEDIUM', 'Medium'
    HIGH = 'HIGH', 'High'


class RequestStatus(models.TextChoices):
    #: Waiting at a stage of its workflow.
    IN_APPROVAL = 'IN_APPROVAL', 'In approval'
    #: Sent back to its creator (from an approval stage) to edit and resubmit.
    RETURNED = 'RETURNED', 'Returned to creator'
    #: Final stage approved: the money may now be transferred.
    COMPLETED = 'COMPLETED', 'Completed'
    #: Rejected at any stage. Terminal; a posted voucher is cancelled.
    REJECTED = 'REJECTED', 'Rejected'
    #: Withdrawn by its creator before any approval. Terminal.
    CANCELLED = 'CANCELLED', 'Cancelled'


class StageRole(models.TextChoices):
    """What a stage DOES, told from its name in the Workflows page.

    Every ADVANCE_PAYMENT workflow ends with three stages named exactly
    Payment Approval, Audit Approval, Final Approval, in that order. Before
    them it may have ANY number of approval stages (none, one, five), named
    as you like: Sub-HOD, HOD, Director, anything. All of those are APPROVAL.
    """

    #: Any stage before Payment: approve, reject, or return to the creator.
    APPROVAL = 'APPROVAL', 'Approval'
    #: Fills the payment details; approves only when they are complete.
    PAYMENT = 'PAYMENT', 'Payment Approval'
    #: Approves or rejects; nothing reaches SAP yet.
    AUDIT = 'AUDIT', 'Audit Approval'
    #: Approving posts the voucher to SAP, then completes: the money may go.
    #: May send back to Payment instead.
    FINAL = 'FINAL', 'Final Approval'

    @classmethod
    def of(cls, stage_name):
        """The role of an engine stage, by its name (case and spacing ignored).

        Payment / Audit / Final by their exact names; every other name is a
        general APPROVAL stage.
        """
        key = ' '.join(str(stage_name or '').split()).lower()
        for role in (cls.PAYMENT, cls.AUDIT, cls.FINAL):
            if role.label.lower() == key:
                return role
        return cls.APPROVAL


#: The fixed stages, in the order a route must end with them.
FIXED_ROLES = (StageRole.PAYMENT, StageRole.AUDIT, StageRole.FINAL)

#: The stages that may send a request back to its creator.
RETURN_TO_CREATOR_ROLES = (StageRole.APPROVAL,)


class AdvanceRequest(models.Model):
    """One payment request, as its creator raised it.

    Company, department and sub-department are what the Workflow Engine's
    queries read to choose the approval route:

        SELECT id FROM advance_payment_request
        WHERE department_id = 35 AND sub_department_id IN (87, 92)

    The creator may edit or cancel it only while no stage has approved it in
    the current round, or while it is RETURNED to them (see `RequestFlow`).
    """

    #: AP-2026-0001: per calendar year, assigned at submission.
    request_no = models.CharField(max_length=20, unique=True)
    company = models.CharField(max_length=20, choices=COMPANY_CHOICES, db_index=True)
    request_type = models.CharField(max_length=20, choices=RequestType.choices)
    payment_against = models.CharField(max_length=20, choices=PaymentAgainst.choices)
    payment_against_other = models.CharField(max_length=120, blank=True, default='')

    # ── Routing: read by the workflow queries ──────────────────────────────
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, related_name='requests')
    sub_department = models.ForeignKey(
        SubDepartment, on_delete=models.PROTECT, null=True, blank=True, related_name='requests',
        help_text='Empty for a department that has no sub-departments.')

    # ── Who is paid ───────────────────────────────────────────────────────
    #: SAP CardCode (vendor / imprest) or the employee-advance G/L account.
    partner_code = models.CharField(max_length=50)
    partner_name = models.CharField(max_length=200)
    #: An employee from the employee master with no advance account in SAP
    #: yet: the Payment stage cannot approve until it exists.
    partner_not_in_sap = models.BooleanField(default=False)

    # ── How much ──────────────────────────────────────────────────────────
    #: The request's total: the documents' lines, or the typed amount.
    amount = models.DecimalField(max_digits=19, decimal_places=2)
    currency = models.CharField(max_length=3, default='INR')

    # ── Dates and repayment, as the form asks them ────────────────────────
    expected_date = models.DateField(null=True, blank=True, help_text='Against PO: Expected Bill Date.')
    expected_bill_date = models.DateField(null=True, blank=True, help_text='Employee Imprest.')
    return_method = models.CharField(max_length=20, choices=ReturnMethod.choices, blank=True, default='')
    return_method_other = models.CharField(max_length=120, blank=True, default='')
    installments = models.PositiveSmallIntegerField(null=True, blank=True)
    emi_amount = models.DecimalField(max_digits=19, decimal_places=2, null=True, blank=True)
    expected_from_date = models.DateField(null=True, blank=True)
    expected_to_date = models.DateField(null=True, blank=True)
    payment_date = models.DateField()
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.MEDIUM)
    remarks = models.TextField(blank=True, default='')

    # ── Ownership: information only, no say in the approval ──────────────
    owner_employee = models.ForeignKey(
        Employee, on_delete=models.PROTECT, null=True, blank=True, related_name='owned_requests')
    #: "Preshit Singh (JWPL0030)", as shown when raised.
    owner_label = models.CharField(max_length=200, blank=True, default='')

    # ── State ─────────────────────────────────────────────────────────────
    status = models.CharField(
        max_length=20, choices=RequestStatus.choices, default=RequestStatus.IN_APPROVAL, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='advance_requests')
    created_on = models.DateTimeField(default=timezone.now, db_index=True)
    updated_on = models.DateTimeField(auto_now=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    cancelled_on = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'advance_payment_request'
        ordering = ['-created_on']
        indexes = [
            models.Index(fields=['company', 'status'], name='ap_request_company_status_idx'),
            models.Index(fields=['department', 'sub_department'], name='ap_request_dept_idx'),
            models.Index(fields=['created_by', 'status'], name='ap_request_creator_idx'),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name='ap_request_amount_positive'),
        ]

    def __str__(self):
        return f'{self.request_no} {self.partner_name}'


class DocumentKind(models.TextChoices):
    BILL = 'BILL', 'A/P invoice'
    PO = 'PO', 'Purchase order'
    OTHER = 'OTHER', 'Other open document'


class AllocationMode(models.TextChoices):
    FIXED = 'FIXED', 'Fixed amount'
    PERCENT = 'PERCENT', 'Percentage'


class RequestDocument(models.Model):
    """One SAP bill / PO the request pays, as it stood when chosen.

    A SNAPSHOT: open amounts move as SAP is paid, and a request must keep
    saying what its creator saw. The SAP document is identified by
    `(company, kind, sap_doc_entry)`.
    """

    request = models.ForeignKey(AdvanceRequest, on_delete=models.CASCADE, related_name='documents')
    kind = models.CharField(max_length=10, choices=DocumentKind.choices)
    sap_doc_entry = models.IntegerField()
    sap_doc_num = models.CharField(max_length=30, blank=True, default='')
    #: Other open documents (goods receipts, credit memos…): SAP's object type.
    sap_doc_type = models.CharField(max_length=60, blank=True, default='')
    vendor_ref = models.CharField(max_length=100, blank=True, default='')
    doc_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    original_amount = models.DecimalField(max_digits=19, decimal_places=2)
    paid_amount = models.DecimalField(max_digits=19, decimal_places=2)
    open_amount = models.DecimalField(max_digits=19, decimal_places=2)
    mode = models.CharField(max_length=10, choices=AllocationMode.choices, default=AllocationMode.FIXED)
    percentage = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    #: What this line pays, in rupees, whichever mode it was entered in.
    amount = models.DecimalField(max_digits=19, decimal_places=2)
    #: The document's latest SAP attachment at the time, and what reading it
    #: found (`invoice_fields.extract`'s output): evidence for the approvers.
    attachment_file = models.CharField(max_length=255, blank=True, default='')
    #: How many attachments the SAP document had, and the latest one's date.
    attachment_count = models.PositiveSmallIntegerField(default=0)
    attachment_date = models.DateField(null=True, blank=True)
    attachment_check = models.JSONField(null=True, blank=True, default=None)

    class Meta:
        db_table = 'advance_payment_request_document'
        ordering = ['request', 'id']
        constraints = [
            models.UniqueConstraint(fields=['request', 'kind', 'sap_doc_entry'],
                                    name='ap_request_document_once'),
            models.CheckConstraint(condition=Q(amount__gt=0), name='ap_request_document_amount_positive'),
            models.CheckConstraint(condition=Q(amount__lte=F('open_amount')),
                                   name='ap_request_document_within_open'),
        ]

    def __str__(self):
        return f'{self.request_id}: {self.kind} {self.sap_doc_num}'


class FilePurpose(models.TextChoices):
    #: Raised with the request: quotations, approvals, bills.
    SUPPORTING = 'SUPPORTING', 'Supporting document'
    #: The payee's bank details: a cancelled cheque, a bank letter.
    BANK_PROOF = 'BANK_PROOF', 'Bank proof'
    #: After paying: the bank's advice, a screenshot, a statement.
    PAYMENT_PROOF = 'PAYMENT_PROOF', 'Payment proof'


def _request_file_path(instance, filename):
    return f'advance_payment/{instance.request_id}/{filename}'


class RequestFile(models.Model):
    """A file uploaded to a request, by whoever and at whichever stage."""

    request = models.ForeignKey(AdvanceRequest, on_delete=models.CASCADE, related_name='files')
    payout_line = models.ForeignKey(
        'PayoutLine', on_delete=models.SET_NULL, null=True, blank=True, related_name='files')
    purpose = models.CharField(max_length=20, choices=FilePurpose.choices)
    file = models.FileField(upload_to=_request_file_path, max_length=500)
    name = models.CharField(max_length=255)
    size = models.PositiveIntegerField()
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    uploaded_on = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'advance_payment_request_file'
        ordering = ['request', 'uploaded_on']

    def __str__(self):
        return self.name


class PayoutMethod(models.TextChoices):
    UPI = 'UPI', 'UPI'
    NEFT = 'NEFT', 'NEFT'
    RTGS = 'RTGS', 'RTGS'
    IMPS = 'IMPS', 'IMPS'
    CHEQUE = 'CHEQUE', 'Cheque'
    CASH = 'CASH', 'Cash'


class Payout(models.Model):
    """How the request is paid: filled in at the Payment stage. One per request."""

    request = models.OneToOneField(AdvanceRequest, on_delete=models.CASCADE, related_name='payout')
    beneficiary_name = models.CharField(max_length=200)
    to_account_number = models.CharField(max_length=40, blank=True, default='')
    to_ifsc = models.CharField(max_length=11, blank=True, default='')
    #: Typed by hand rather than chosen from the payee's SAP bank accounts.
    to_account_manual = models.BooleanField(default=False)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    updated_on = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'advance_payment_payout'

    def __str__(self):
        return f'{self.request_id} payout'


class PayoutLine(models.Model):
    """One method of paying: the lines together add up to the request's amount."""

    payout = models.ForeignKey(Payout, on_delete=models.CASCADE, related_name='lines')
    method = models.CharField(max_length=10, choices=PayoutMethod.choices)
    amount = models.DecimalField(max_digits=19, decimal_places=2)
    #: OUR account the money leaves: a bank G/L, or a cash G/L for cash.
    from_account = models.CharField(max_length=30)
    from_account_label = models.CharField(max_length=200, blank=True, default='')
    cheque_number = models.CharField(max_length=20, blank=True, default='')
    cheque_bank = models.CharField(max_length=100, blank=True, default='')
    cheque_date = models.DateField(null=True, blank=True)
    #: Cash only: [{"denomination": 500, "quantity": 18}, ...]
    cash_notes = models.JSONField(null=True, blank=True, default=None)
    #: After paying (transfers only): the bank's reference, and how it was
    #: established (`payment_proof.extract`'s output when read from a proof).
    utr = models.CharField(max_length=30, blank=True, default='')
    utr_proof = models.JSONField(null=True, blank=True, default=None)
    utr_recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    utr_recorded_on = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'advance_payment_payout_line'
        ordering = ['payout', 'id']
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name='ap_payout_line_amount_positive'),
        ]

    def __str__(self):
        return f'{self.method} {self.amount}'


class FlowStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    RETURNED = 'RETURNED', 'Returned to creator'
    COMPLETED = 'COMPLETED', 'Completed'
    REJECTED = 'REJECTED', 'Rejected'
    CANCELLED = 'CANCELLED', 'Cancelled'


class RequestFlow(models.Model):
    """Where one request is in its workflow. ONE ROW PER REQUEST.

    `current_stage` is the authority for "where is it?"; "who may act?" is
    `get_stage_assignment(current_stage)` (today's user, after replacements).
    `current_user` is a copy kept only so queues can be listed without
    resolving every row, exactly as PRDO does.

    `cycle` counts rounds: +1 when the creator resubmits after a return, and
    +1 when Final sends back to Payment. "No approval yet" (the creator's edit
    and cancel rule) means: no APPROVED log row in the current cycle.
    """

    request = models.OneToOneField(AdvanceRequest, on_delete=models.CASCADE, related_name='flow')
    #: The workflow the engine chose, and the query that chose it. PROTECT:
    #: deleting configuration that explains a past decision rewrites history.
    workflow = models.ForeignKey('workflow.Workflow', on_delete=models.PROTECT, related_name='+')
    matched_query = models.ForeignKey(
        'workflow.WorkflowQuery', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    status = models.CharField(
        max_length=10, choices=FlowStatus.choices, default=FlowStatus.PENDING, db_index=True)
    #: The stage waiting for a decision; NULL once returned or finished.
    current_stage = models.ForeignKey(
        'workflow.WorkflowStage', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    #: `StageRole` of `current_stage`, stored so queues can filter by it.
    current_role = models.CharField(max_length=10, choices=StageRole.choices, blank=True, default='')
    current_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    #: Stage count of the chosen workflow at submission ("stage 2 of 6").
    total_stages = models.PositiveSmallIntegerField(default=0)
    cycle = models.PositiveSmallIntegerField(default=1)
    #: UNUSED since posting moved to Final approval (nothing is in SAP to
    #: re-post before then). Kept rather than dropped: no schema change.
    edited_since_audit = models.BooleanField(default=False)
    #: Optimistic lock: bumped on every move, so two approvers acting on the
    #: same request at once cannot both succeed.
    version = models.PositiveIntegerField(default=1)
    created_on = models.DateTimeField(default=timezone.now)
    updated_on = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'advance_payment_request_flow'
        indexes = [
            models.Index(fields=['status', 'current_user'], name='ap_flow_queue_idx'),
            models.Index(fields=['current_stage'], name='ap_flow_stage_idx'),
            models.Index(fields=['workflow'], name='ap_flow_workflow_idx'),
        ]

    def __str__(self):
        return f'{self.request_id} [{self.status}] cycle {self.cycle}'


class VoucherObject(models.TextChoices):
    OUTGOING_PAYMENT = 'OUTGOING_PAYMENT', 'Outgoing payment (OVPM)'
    JOURNAL_ENTRY = 'JOURNAL_ENTRY', 'Journal entry (OJDT)'


class VoucherStatus(models.TextChoices):
    POSTED = 'POSTED', 'Posted'
    FAILED = 'FAILED', 'Failed'
    CANCELLED = 'CANCELLED', 'Cancelled'


class SapVoucher(models.Model):
    """Every attempt to post the request's voucher to SAP, and its fate.

    SAP B1 cannot edit a posted outgoing payment or journal entry, so an
    "update" is: cancel this one, post a new one, and point this row's
    `replaced_by` at it. At most one row per request is POSTED and not
    replaced at any time: that is the live voucher.
    """

    request = models.ForeignKey(AdvanceRequest, on_delete=models.CASCADE, related_name='vouchers')
    #: 1, 2, … per request.
    version = models.PositiveSmallIntegerField()
    sap_object = models.CharField(max_length=20, choices=VoucherObject.choices)
    status = models.CharField(max_length=10, choices=VoucherStatus.choices)
    sap_doc_entry = models.IntegerField(null=True, blank=True)
    sap_doc_num = models.IntegerField(null=True, blank=True)
    #: Exactly what was sent to SAP, and exactly what SAP answered: a generic
    #: "failed to post" is undiagnosable (PRDO's lesson).
    payload = models.JSONField(null=True, blank=True, default=None)
    response = models.JSONField(null=True, blank=True, default=None)
    error = models.TextField(blank=True, default='')
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    posted_on = models.DateTimeField(default=timezone.now)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    cancelled_on = models.DateTimeField(null=True, blank=True)
    #: SAP's cancellation document, where SAP makes one.
    cancel_doc_entry = models.IntegerField(null=True, blank=True)
    replaced_by = models.OneToOneField(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='replaces')

    class Meta:
        db_table = 'advance_payment_sap_voucher'
        ordering = ['request', 'version']
        constraints = [
            models.UniqueConstraint(fields=['request', 'version'], name='ap_voucher_version_uq'),
            # One live voucher per request.
            models.UniqueConstraint(
                fields=['request'], condition=Q(status='POSTED', replaced_by__isnull=True),
                name='ap_voucher_one_live'),
        ]

    def __str__(self):
        return f'{self.request_id} v{self.version} {self.status}'


class LogAction(models.TextChoices):
    CREATED = 'CREATED', 'Created'
    SUBMITTED = 'SUBMITTED', 'Submitted'
    EDITED = 'EDITED', 'Edited'
    CANCELLED = 'CANCELLED', 'Cancelled'
    RETURNED = 'RETURNED', 'Returned to creator'
    RESUBMITTED = 'RESUBMITTED', 'Resubmitted'
    APPROVED = 'APPROVED', 'Approved'
    REJECTED = 'REJECTED', 'Rejected'
    SENT_BACK = 'SENT_BACK', 'Sent back to Payment'
    PAYOUT_UPDATED = 'PAYOUT_UPDATED', 'Payment details updated'
    PARTNER_LINKED = 'PARTNER_LINKED', 'Linked to its SAP account'
    SAP_POSTED = 'SAP_POSTED', 'Voucher posted to SAP'
    SAP_POST_FAILED = 'SAP_POST_FAILED', 'Voucher failed to post'
    SAP_CANCELLED = 'SAP_CANCELLED', 'Voucher cancelled in SAP'
    SAP_REPOSTED = 'SAP_REPOSTED', 'Voucher re-posted to SAP'
    UTR_RECORDED = 'UTR_RECORDED', 'UTR recorded'
    FILE_ADDED = 'FILE_ADDED', 'File added'
    FILE_REMOVED = 'FILE_REMOVED', 'File removed'
    COMPLETED = 'COMPLETED', 'Completed'


class RequestLog(models.Model):
    """The request's whole lifecycle. APPEND-ONLY: nothing updates or deletes a row.

    Stage name and sequence are copied in as well as the stage FK: a stage
    renamed or retired later must not change what history says happened.
    """

    request = models.ForeignKey(AdvanceRequest, on_delete=models.CASCADE, related_name='logs')
    action = models.CharField(max_length=20, choices=LogAction.choices)
    cycle = models.PositiveSmallIntegerField(default=1)
    stage = models.ForeignKey(
        'workflow.WorkflowStage', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    stage_name = models.CharField(max_length=120, blank=True, default='')
    stage_sequence = models.PositiveSmallIntegerField(null=True, blank=True)
    #: Who did it. NULL only for the system (none today).
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    #: When a replacement acted: the stage's configured user they stood in for.
    on_behalf_of = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    from_status = models.CharField(max_length=20, blank=True, default='')
    to_status = models.CharField(max_length=20, blank=True, default='')
    remarks = models.TextField(blank=True, default='')
    #: What changed ({"amount": {"old": "…", "new": "…"}}), SAP document
    #: numbers, the voucher version…: whatever makes the row self-explaining.
    data = models.JSONField(null=True, blank=True, default=None)
    created_on = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = 'advance_payment_request_log'
        ordering = ['created_on', 'id']
        indexes = [
            models.Index(fields=['request', 'created_on'], name='ap_log_request_idx'),
            models.Index(fields=['request', 'cycle', 'action'], name='ap_log_cycle_action_idx'),
        ]

    def __str__(self):
        return f'{self.request_id} {self.action}'
