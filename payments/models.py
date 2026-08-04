"""Receive Payment and Bank Deposit domain models.

Two conventions carried through every table here, both correcting defects found
in the existing code:

  * `company` is the SAP category (OIL / BEVERAGES / MART) and is stored on the
    row, not derived. In this system the same `card_code` is a DIFFERENT
    business partner per company (see orders/views.py:2732-2733), so
    (card_code, company) is the only safe identity.
  * Money invariants are DB CheckConstraints, not only serializer rules. The
    project currently has zero CheckConstraints, which means any write path
    that bypasses the serializer bypasses validation entirely.
"""
from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.fields import GenericRelation
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import F, Q

from core.models import TimeStampedModel

CATEGORY_CHOICES = [
    ('OIL', 'Oil'),
    ('BEVERAGES', 'Beverages'),
    ('MART', 'Mart'),
]


# ---------------------------------------------------------------------------
# Masters
# ---------------------------------------------------------------------------

class SapCompanyMap(models.Model):
    """category -> SAP company DB / HANA schema.

    Replaces `resolve_company_db_for_order` (sap_sync/services/sync_service.py:301),
    which derives the DB from ITEM category (a payment has no items), only
    handles BEVERAGES, and silently routes MART to the OIL database. It also
    supplies the allow-list that makes schema interpolation in raw SQL safe.
    """

    company = models.CharField(max_length=20, choices=CATEGORY_CHOICES, unique=True)
    display_name = models.CharField(max_length=100)
    company_db = models.CharField(max_length=100)
    hana_schema = models.CharField(max_length=100)
    default_bpl_id = models.IntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = 'payment_sap_company_map'
        ordering = ['sort_order', 'company']
        verbose_name = 'SAP company mapping'
        verbose_name_plural = 'SAP company mappings'

    def __str__(self):
        return f'{self.company} -> {self.company_db}'


class CollectionPerson(TimeStampedModel):
    """The manually-maintained "Received From" list.

    These are our own staff/collection agents, not SAP business partners, so
    there is nothing to sync. Confirms the requirement: `Order.employee_id` is
    free text referencing nothing (orders/models.py:132), and OSLP has no list
    query.
    """

    name = models.CharField(max_length=120)
    code = models.CharField(max_length=30, unique=True)
    # Blank = selectable in every company.
    company = models.CharField(
        max_length=20, choices=CATEGORY_CHOICES, blank=True, default='')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='collection_profiles')
    sap_slp_code = models.IntegerField(null=True, blank=True)   # optional OSLP link
    phone = models.CharField(max_length=20, blank=True, default='')
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'payment_collection_person'
        ordering = ['name']
        indexes = [models.Index(fields=['company', 'is_active'],
                                name='idx_collperson_company')]

    def __str__(self):
        return self.name


class BankAccount(models.Model):
    """Our own bank / cash accounts, and their SAP GL codes."""

    class AccountType(models.TextChoices):
        BANK = 'BANK', 'Bank'
        CASH = 'CASH', 'Cash'

    name = models.CharField(max_length=150)          # 'HDFC Bank — ****4821'
    company = models.CharField(max_length=20, choices=CATEGORY_CHOICES, db_index=True)
    account_type = models.CharField(
        max_length=10, choices=AccountType.choices, default=AccountType.BANK)

    # SAP posting targets.
    sap_gl_account = models.CharField(max_length=50)

    masked_number = models.CharField(max_length=50, blank=True, default='')
    ifsc = models.CharField(max_length=20, blank=True, default='')
    branch_name = models.CharField(max_length=150, blank=True, default='')
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'payment_bank_account'
        ordering = ['company', 'name']
        constraints = [
            models.UniqueConstraint(fields=['company', 'sap_gl_account'],
                                    name='payment_bank_gl_per_company_uq'),
        ]

    def __str__(self):
        return f'{self.name} ({self.company})'


# ---------------------------------------------------------------------------
# Receive Payment
# ---------------------------------------------------------------------------

class PaymentReceipt(TimeStampedModel):
    """A payment received from a party (SAP: IncomingPayments / ORCT)."""

    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        PENDING_APPROVAL = 'PENDING_APPROVAL', 'Pending approval'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'
        # Set immediately before the SAP call and cleared by its outcome. A row
        # left here means the process died mid-call — see SAP_UNKNOWN.
        POSTING_TO_SAP = 'POSTING_TO_SAP', 'Posting to SAP'
        POSTED = 'POSTED', 'Posted to SAP'
        # SAP ANSWERED with an error. Safe to edit and resubmit: nothing was
        # committed on the SAP side.
        PENDING_ERROR = 'PENDING_ERROR', 'Pending error'
        # SAP never answered (timeout / connection lost). The document may or
        # may not exist in SAP, so resubmitting could duplicate a payment.
        # Reconciliation resolves it; the creator cannot resubmit meanwhile.
        SAP_UNKNOWN = 'SAP_UNKNOWN', 'Awaiting SAP verification'
        CANCELLED = 'CANCELLED', 'Cancelled'

    class ReceivedFromType(models.TextChoices):
        PARTY = 'PARTY', 'Party'
        PERSON = 'PERSON', 'Company person'

    receipt_no = models.CharField(max_length=40, unique=True)

    company = models.CharField(max_length=20, choices=CATEGORY_CHOICES, db_index=True)
    # Frozen at creation. Deriving it at post time would mean an .env edit
    # between draft and post silently posts to a different company.
    company_db = models.CharField(max_length=100, blank=True, default='')

    card_code = models.CharField(max_length=50, db_index=True)
    card_name = models.CharField(max_length=200, blank=True, default='')

    received_from_type = models.CharField(
        max_length=10, choices=ReceivedFromType.choices,
        default=ReceivedFromType.PARTY)
    received_from_person = models.ForeignKey(
        CollectionPerson, on_delete=models.PROTECT,
        null=True, blank=True, related_name='receipts')

    payment_date = models.DateField(db_index=True)
    is_advance = models.BooleanField(default=False)
    # Derived from the method rows inside the create transaction — never taken
    # from the client payload (which is what orders/views.py:2530 does, leaving
    # total_amount disagreeing with SUM(items) after a partial write).
    total_amount = models.DecimalField(
        max_digits=15, decimal_places=2,
        validators=[MinValueValidator(Decimal('0.01'))])
    allocated_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default='INR')

    remarks = models.TextField(blank=True, default='')
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)

    # SAP write-back
    sap_doc_entry = models.IntegerField(null=True, blank=True, db_index=True)
    sap_doc_num = models.IntegerField(null=True, blank=True)
    sap_posted_at = models.DateTimeField(null=True, blank=True)
    # SAP's exact words from the last posting attempt — the success
    # confirmation or the rejection reason. Shown verbatim in the UI so a user
    # can act on it without database access or developer help.
    sap_response = models.TextField(blank=True, default='')

    # The receipt -> deposit link lives ONLY on BankDepositLine, which carries
    # the UniqueConstraint that stops a receipt being banked twice. A second FK
    # here would be an unconstrained parallel path to the same fact, free to
    # disagree with the line table.

    attachments = GenericRelation('attachments.Attachment',
                                  related_query_name='payment_receipt')
    approvals = GenericRelation('approvals.ApprovalRequest',
                                related_query_name='payment_receipt')

    class Meta:
        db_table = 'payment_receipt'
        ordering = ['-payment_date', '-id']
        indexes = [
            models.Index(fields=['company', 'status', '-payment_date'],
                         name='idx_rcpt_company_status'),
            models.Index(fields=['card_code', 'company'], name='idx_rcpt_party'),
            models.Index(fields=['status', 'sap_doc_entry'], name='idx_rcpt_sap'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(total_amount__gt=0),
                name='payment_receipt_amount_positive'),
            models.CheckConstraint(
                condition=Q(allocated_amount__gte=0)
                & Q(allocated_amount__lte=F('total_amount')),
                name='payment_receipt_allocated_within_total'),
            models.CheckConstraint(
                condition=Q(received_from_type='PARTY')
                | Q(received_from_person__isnull=False),
                name='payment_receipt_person_required'),
        ]

    def __str__(self):
        return f'{self.receipt_no} — {self.card_name or self.card_code}'

    @property
    def unallocated_amount(self):
        return self.total_amount - self.allocated_amount

    def can_be_viewed_by(self, user):
        """Used by the attachment download guard."""
        if user.is_superuser or user.is_staff:
            return True
        if self.created_by_id == user.id:
            return True
        # Anyone who can act on (or has acted on) its approval may see the file.
        return self.approvals.filter(
            Q(actions__approver=user) | Q(status='PENDING')).exists()


class PaymentMethodEntry(models.Model):
    """One tender line on a receipt: cash, UPI or cheque."""

    class Method(models.TextChoices):
        CASH = 'CASH', 'Cash'
        UPI = 'UPI', 'UPI'
        CHEQUE = 'CHEQUE', 'Cheque'

    receipt = models.ForeignKey(
        PaymentReceipt, on_delete=models.CASCADE, related_name='methods')
    method = models.CharField(max_length=10, choices=Method.choices)
    amount = models.DecimalField(max_digits=15, decimal_places=2)

    # UPI
    upi_reference = models.CharField(max_length=60, blank=True, default='')
    # Cheque
    cheque_number = models.CharField(max_length=30, blank=True, default='')
    bank_name = models.CharField(max_length=120, blank=True, default='')
    cheque_date = models.DateField(null=True, blank=True)
    # Set from the posted SAP payment's PaymentChecks[].CheckKey; a deposit
    # references an existing cheque by this key, so it can only be banked once
    # the receipt itself has posted.
    sap_check_key = models.IntegerField(null=True, blank=True)

    class Meta:
        db_table = 'payment_method_entry'
        ordering = ['id']
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0),
                                   name='payment_method_amount_positive'),
            models.CheckConstraint(
                condition=~Q(method='CHEQUE')
                | (~Q(cheque_number='') & Q(cheque_date__isnull=False)),
                name='payment_method_cheque_requires_details'),
        ]

    def __str__(self):
        return f'{self.method} {self.amount}'


class CashDenomination(models.Model):
    """Note breakdown for a cash tender line."""

    DENOMINATIONS = [10, 20, 50, 100, 200, 500]

    entry = models.ForeignKey(
        PaymentMethodEntry, on_delete=models.CASCADE, related_name='denominations')
    denomination = models.PositiveSmallIntegerField(
        choices=[(d, f'₹{d}') for d in DENOMINATIONS])
    quantity = models.PositiveIntegerField()

    class Meta:
        db_table = 'payment_cash_denomination'
        ordering = ['-denomination']
        constraints = [
            models.UniqueConstraint(fields=['entry', 'denomination'],
                                    name='payment_denomination_uq'),
            models.CheckConstraint(condition=Q(quantity__gt=0),
                                   name='payment_denomination_qty_positive'),
        ]

    def __str__(self):
        return f'₹{self.denomination} x {self.quantity}'

    @property
    def line_total(self):
        return Decimal(self.denomination) * self.quantity


class PaymentAllocation(models.Model):
    """Applies part of a receipt to one open SAP invoice.

    Maps 1:1 to an entry in SAP's PaymentInvoices[]. There is no local invoice
    table — invoices live in SAP, so this stores the DocEntry plus a snapshot
    taken at selection time.
    """

    receipt = models.ForeignKey(
        PaymentReceipt, on_delete=models.CASCADE, related_name='allocations')
    sap_doc_entry = models.IntegerField()
    sap_doc_num = models.IntegerField(null=True, blank=True)
    # SAP InvoiceType: 13 = it_Invoice, 14 = it_CredItnote.
    invoice_type = models.IntegerField(default=13)

    invoice_date = models.DateField(null=True, blank=True)
    invoice_due_date = models.DateField(null=True, blank=True)
    invoice_total = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    # Snapshot so the worker can detect that SAP changed underneath us between
    # selection and posting, and fail loudly instead of over-applying.
    balance_at_selection = models.DecimalField(
        max_digits=15, decimal_places=2, default=0)

    amount_applied = models.DecimalField(max_digits=15, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'payment_invoice_allocation'
        ordering = ['invoice_due_date', 'sap_doc_num']
        constraints = [
            models.UniqueConstraint(
                fields=['receipt', 'sap_doc_entry', 'invoice_type'],
                name='payment_allocation_uq'),
            models.CheckConstraint(condition=Q(amount_applied__gt=0),
                                   name='payment_allocation_amount_positive'),
        ]
        indexes = [models.Index(fields=['sap_doc_entry'], name='idx_alloc_docentry')]

    def __str__(self):
        return f'INV {self.sap_doc_num} <- {self.amount_applied}'


# ---------------------------------------------------------------------------
# Bank Deposit
# ---------------------------------------------------------------------------

class BankDeposit(TimeStampedModel):
    """Banking one or more receipts (SAP: Deposits / ODPS)."""

    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        PENDING_APPROVAL = 'PENDING_APPROVAL', 'Pending approval'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'
        # Set immediately before the SAP call and cleared by its outcome. A row
        # left here means the process died mid-call — see SAP_UNKNOWN.
        POSTING_TO_SAP = 'POSTING_TO_SAP', 'Posting to SAP'
        POSTED = 'POSTED', 'Posted to SAP'
        # SAP ANSWERED with an error. Safe to edit and resubmit: nothing was
        # committed on the SAP side.
        PENDING_ERROR = 'PENDING_ERROR', 'Pending error'
        # SAP never answered (timeout / connection lost). The document may or
        # may not exist in SAP, so resubmitting could duplicate a payment.
        # Reconciliation resolves it; the creator cannot resubmit meanwhile.
        SAP_UNKNOWN = 'SAP_UNKNOWN', 'Awaiting SAP verification'
        CANCELLED = 'CANCELLED', 'Cancelled'

    class DepositType(models.TextChoices):
        CASH = 'CASH', 'Cash'
        CHEQUE = 'CHEQUE', 'Cheque'
        MIXED = 'MIXED', 'Mixed (cash + cheque)'

    deposit_no = models.CharField(max_length=40, unique=True)

    company = models.CharField(max_length=20, choices=CATEGORY_CHOICES, db_index=True)
    company_db = models.CharField(max_length=100, blank=True, default='')

    deposit_date = models.DateField(db_index=True)
    deposited_by = models.ForeignKey(
        CollectionPerson, on_delete=models.PROTECT,
        null=True, blank=True, related_name='deposits')
    bank_account = models.ForeignKey(
        BankAccount, on_delete=models.PROTECT, related_name='deposits')
    deposit_type = models.CharField(
        max_length=10, choices=DepositType.choices, default=DepositType.CASH)

    # Sum of the linked receipts, computed in the service.
    collected_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    # What was actually banked. May be less; then a reason is mandatory.
    deposit_amount = models.DecimalField(max_digits=15, decimal_places=2)
    shortfall_reason = models.TextField(blank=True, default='')
    bank_charge = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default='INR')

    slip_number = models.CharField(max_length=100, blank=True, default='')
    remarks = models.TextField(blank=True, default='')
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)

    sap_doc_entry = models.IntegerField(null=True, blank=True, db_index=True)
    sap_doc_num = models.IntegerField(null=True, blank=True)
    sap_posted_at = models.DateTimeField(null=True, blank=True)
    # SAP's exact words from the last posting attempt — the success
    # confirmation or the rejection reason. Shown verbatim in the UI so a user
    # can act on it without database access or developer help.
    sap_response = models.TextField(blank=True, default='')

    attachments = GenericRelation('attachments.Attachment',
                                  related_query_name='bank_deposit')
    approvals = GenericRelation('approvals.ApprovalRequest',
                                related_query_name='bank_deposit')

    class Meta:
        db_table = 'payment_bank_deposit'
        ordering = ['-deposit_date', '-id']
        indexes = [
            models.Index(fields=['company', 'status', '-deposit_date'],
                         name='idx_dep_company_status'),
            models.Index(fields=['bank_account', '-deposit_date'],
                         name='idx_dep_bank_date'),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(deposit_amount__gt=0),
                                   name='bank_deposit_amount_positive'),
            # These two encode exactly the rules already enforced in the mobile
            # UI, so the API cannot be bypassed to store an invalid deposit.
            models.CheckConstraint(
                condition=Q(deposit_amount__lte=F('collected_amount')),
                name='bank_deposit_not_over_collected'),
            models.CheckConstraint(
                condition=Q(deposit_amount=F('collected_amount'))
                | ~Q(shortfall_reason=''),
                name='bank_deposit_shortfall_requires_reason'),
        ]

    def __str__(self):
        return f'{self.deposit_no} — {self.deposit_amount}'

    @property
    def shortfall(self):
        return self.collected_amount - self.deposit_amount

    def can_be_viewed_by(self, user):
        if user.is_superuser or user.is_staff:
            return True
        if self.created_by_id == user.id:
            return True
        return self.approvals.filter(
            Q(actions__approver=user) | Q(status='PENDING')).exists()


class BankDepositLine(models.Model):
    """One receipt inside a deposit."""

    deposit = models.ForeignKey(
        BankDeposit, on_delete=models.CASCADE, related_name='lines')
    receipt = models.ForeignKey(
        PaymentReceipt, on_delete=models.PROTECT, related_name='deposit_lines')
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'payment_bank_deposit_line'
        constraints = [
            # A receipt can never appear in two deposits — the money-safety
            # constraint on the deposit side.
            models.UniqueConstraint(fields=['receipt'],
                                    name='bank_deposit_receipt_once_uq'),
        ]
        indexes = [models.Index(fields=['deposit'], name='idx_depline_deposit')]

    def __str__(self):
        return f'{self.deposit_id} <- {self.receipt_id}'


# ---------------------------------------------------------------------------
# SAP integration
# ---------------------------------------------------------------------------

class SapCallLog(models.Model):
    """One row per HTTP call to the Service Layer.

    Follows the shape of sap_sync.SalesOrderLog (sap_sync/models.py:201) but
    stores PARSED json on failure too (the original writes raw response.text
    into a JSONField, so success and failure rows have different shapes), and
    redacts bank/cheque detail before storing.
    """

    class Status(models.TextChoices):
        STARTED = 'STARTED', 'Started'
        SUCCESS = 'SUCCESS', 'Success'
        FAILED = 'FAILED', 'Failed'

    # Which document this call was for. Generic because both receipts and
    # deposits post; nullable so a log row survives if the document is removed.
    content_type = models.ForeignKey(
        'contenttypes.ContentType', on_delete=models.SET_NULL,
        null=True, blank=True)
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    company_db = models.CharField(max_length=100, blank=True, default='')
    # Always a POST to one of two endpoints, so the verb is not stored. Retries
    # are not stored either: posting is synchronous, so one call is one row, and
    # attempt sequencing across resubmissions lives on SapPostingHistory.
    endpoint = models.CharField(max_length=300)

    request_data = models.JSONField(null=True, blank=True)      # redacted
    response_data = models.JSONField(null=True, blank=True)
    http_status = models.IntegerField(null=True, blank=True)
    sap_error_code = models.CharField(max_length=30, blank=True, default='')
    error_message = models.TextField(blank=True, default='')

    sap_doc_entry = models.IntegerField(null=True, blank=True)
    sap_doc_num = models.IntegerField(null=True, blank=True)

    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.STARTED)
    duration_ms = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'payment_sap_call_log'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', '-created_at'], name='idx_scl_status'),
            models.Index(fields=['content_type', 'object_id'], name='idx_scl_doc'),
        ]

    def __str__(self):
        return f'{self.endpoint} [{self.status}]'


class SapPostingHistory(models.Model):
    """Append-only record of every SAP posting attempt on one receipt.

    `payment_receipt` holds only the LATEST SAP state; this holds the whole
    lifecycle, so a support engineer can answer "how many attempts, when, what
    did SAP say, was it resubmitted, when did it finally succeed, and with which
    DocEntry" from one screen — without server logs or database access.

    Distinct from the two neighbouring logs, which answer different questions:
      * ApprovalAction        — who approved or rejected, and at which level
      * PaymentStatusHistory  — every status transition, including non-SAP ones
      * SapCallLog            — the raw HTTP exchange (payload, headers, timing)

    Rows are NEVER updated or deleted. Corrections are appended.
    """

    class Action(models.TextChoices):
        POST_STARTED = 'POST_STARTED', 'Post started'
        POST_SUCCESS = 'POST_SUCCESS', 'Post succeeded'
        POST_FAILED = 'POST_FAILED', 'Post failed'
        POST_TIMEOUT = 'POST_TIMEOUT', 'Post timed out'
        RESUBMITTED = 'RESUBMITTED', 'Resubmitted after correction'
        MANUAL_RECOVERY = 'MANUAL_RECOVERY', 'Manual recovery'

    class Status(models.TextChoices):
        POSTING = 'POSTING', 'Posting'
        SUCCESS = 'SUCCESS', 'Success'
        FAILED = 'FAILED', 'Failed'
        UNKNOWN = 'UNKNOWN', 'Unknown'

    payment = models.ForeignKey(
        PaymentReceipt, on_delete=models.CASCADE, related_name='sap_history')
    # 1 for the first attempt, rising on each resubmission. Assigned by
    # services.record_sap_history so callers cannot get it out of step.
    attempt_number = models.PositiveSmallIntegerField(default=1)

    action = models.CharField(max_length=20, choices=Action.choices)
    status = models.CharField(max_length=10, choices=Status.choices)

    # Captured on the row itself rather than read back from the receipt: the
    # receipt only ever holds the LATEST keys, so a history row that pointed at
    # it would silently change meaning after the next attempt.
    sap_doc_entry = models.IntegerField(null=True, blank=True)
    sap_doc_num = models.IntegerField(null=True, blank=True)
    sap_response = models.TextField(blank=True, default='')

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='sap_posting_history')
    created_by_username = models.CharField(max_length=150, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'payment_sap_posting_history'
        ordering = ['-created_at', '-id']       # newest first, as the UI shows it
        verbose_name = 'SAP posting history'
        verbose_name_plural = 'SAP posting history'
        indexes = [
            models.Index(fields=['payment', '-created_at'],
                         name='idx_sph_payment'),
        ]

    def __str__(self):
        return f'{self.payment_id} #{self.attempt_number} {self.action}'


class PaymentStatusHistory(models.Model):
    """Append-only lifecycle log for receipts AND deposits.

    Distinct from ApprovalAction: that records approver decisions, this records
    every transition including system-driven ones (QUEUED -> POSTED by the
    worker). Together they answer "log of every action" completely.
    """

    content_type = models.ForeignKey('contenttypes.ContentType',
                                     on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()

    from_status = models.CharField(max_length=20, blank=True, default='')
    to_status = models.CharField(max_length=20)
    reason = models.TextField(blank=True, default='')
    actor_kind = models.CharField(max_length=20, default='USER')   # USER|SYSTEM|SAP_WORKER

    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='payment_status_changes')
    changed_by_username = models.CharField(max_length=150, blank=True, default='')
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'payment_status_history'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['content_type', 'object_id', '-created_at'],
                         name='idx_psh_target'),
        ]

    def __str__(self):
        return f'{self.from_status} -> {self.to_status}'
