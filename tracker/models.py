"""Document (invoice) tracker models.

This app is self-contained: it owns all of its own tables and does NOT hold
foreign keys into any OMS business model. The only shared dependency is the
project user table (settings.AUTH_USER_MODEL), used purely for login,
`created_by` / `acted_by` attribution and per-user stage permissions.

The heart of the design is a small stage machine plus an append-only event
log:

    Invoice  --current_stage-->  Stage
       |
       +----<  StageEvent  (one row per handoff, immutable history)

Every timeline figure (days at a stage, ageing, bottleneck reports, the
"stuck beyond N days" alert) is derived from StageEvent + the live
`current_stage_entered_at` pointer. No date is ever typed by hand after the
invoice is created.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models


# ---------------------------------------------------------------------------
# Lookup tables (admin-managed dropdowns). Kept as concrete tables rather than
# free text so filters, reports and Excel-import mapping stay clean.
# ---------------------------------------------------------------------------
class LookupBase(models.Model):
    """Shared shape for the simple name/active/order dropdown tables."""
    name = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        abstract = True
        ordering = ['sort_order', 'name']

    def __str__(self):
        return self.name


class Category(LookupBase):
    # Transport, RM-PM, Contractor, Fixed Asset, Security,
    # Other Invoice/Consumable, Cash Voucher, Staff Imprest, Refreshment, E-COM
    class Meta(LookupBase.Meta):
        db_table = 'tracker_category'
        verbose_name_plural = 'Categories'


class Unit(LookupBase):
    # Oil, Beverage
    class Meta(LookupBase.Meta):
        db_table = 'tracker_unit'


class Branch(LookupBase):
    # Wellness, Mart
    class Meta(LookupBase.Meta):
        db_table = 'tracker_branch'
        verbose_name_plural = 'Branches'


class InvoiceMode(LookupBase):
    # Mail, Hardcopy
    class Meta(LookupBase.Meta):
        db_table = 'tracker_mode'


class GstType(LookupBase):
    # SGST-CGST, IGST, ISD, NO GST
    class Meta(LookupBase.Meta):
        db_table = 'tracker_gst_type'


class GstRate(models.Model):
    """GST rate carries a numeric value (used in reports) plus a display label."""
    label = models.CharField(max_length=20, unique=True)   # "0%", "5%", "12%" ...
    rate = models.DecimalField(max_digits=5, decimal_places=2, unique=True)  # 0, 5, 12, 18, 28
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = 'tracker_gst_rate'
        ordering = ['sort_order', 'rate']

    def __str__(self):
        return self.label


# ---------------------------------------------------------------------------
# Stage configuration (admin-editable). Reorder / rename / retune thresholds
# without a code change.
# ---------------------------------------------------------------------------
class Stage(models.Model):
    """A single desk in the invoice flow.

    `status_choices` holds the stage-specific dispositions the handler must
    pick from (e.g. Pre-Audit = OK/HOLD/DEBIT/RETURN, SAP Approval =
    APPROVED/REJECTED). Empty for stages that only advance/return.
    """
    name = models.CharField(max_length=100)
    code = models.SlugField(max_length=50, unique=True)   # stable key: entry, bilty_grpo, ...
    order = models.PositiveIntegerField(unique=True)      # 1..N flow position
    threshold_days = models.PositiveIntegerField(default=3)  # "stuck" alert threshold

    # Stage-specific status options + whether one is mandatory here.
    status_choices = models.JSONField(default=list, blank=True)
    requires_status = models.BooleanField(default=False)

    # Whether this desk may bounce an invoice back to the previous stage.
    can_return = models.BooleanField(default=True)
    # Terminal stage (Payment) — nothing advances past it.
    is_terminal = models.BooleanField(default=False)

    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'tracker_stage'
        ordering = ['order']

    def __str__(self):
        return f'{self.order}. {self.name}'


class UserStageAccess(models.Model):
    """Maps a user to the stage(s) they may see and act on.

    Drives both the dashboard queue (a user only sees invoices whose
    current_stage is one they're mapped to) and server-side authorisation of
    advance/return actions.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='tracker_stage_access',
    )
    stage = models.ForeignKey(
        Stage,
        on_delete=models.CASCADE,
        related_name='user_access',
    )
    is_active = models.BooleanField(default=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='tracker_stage_access_granted',
    )
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tracker_user_stage_access'
        unique_together = ['user', 'stage']
        ordering = ['user_id', 'stage__order']

    def __str__(self):
        return f'{self.user_id} @ {self.stage.code}'


# ---------------------------------------------------------------------------
# The tracked document.
# ---------------------------------------------------------------------------
class Invoice(models.Model):
    class Status(models.TextChoices):
        IN_PROGRESS = 'IN_PROGRESS', 'In Progress'
        COMPLETED = 'COMPLETED', 'Completed'

    class AdditionalCharge(models.TextChoices):
        DEMURRAGE = 'DEMURRAGE', 'Demurrage'
        LABOUR_COST = 'LABOUR_COST', 'Labour Cost'
        POINT_VALUE = 'POINT_VALUE', 'Point Value'

    # --- Entry-stage fields (filled by the creator at Stage 1) ---
    invoice_date = models.DateField()
    party_name = models.CharField(max_length=255)
    # SAP vendor (business partner) reference, filled when picked from the SAP
    # vendor dropdown. Free-text party_name is still allowed if not in SAP.
    party_code = models.CharField(max_length=50, blank=True, default='')   # OCRD CardCode
    party_gstin = models.CharField(max_length=20, blank=True, default='')
    invoice_number = models.CharField(max_length=100, unique=True)
    taxable_value = models.DecimalField(max_digits=15, decimal_places=2)
    gst_type = models.ForeignKey(GstType, on_delete=models.PROTECT, related_name='invoices')
    gst_rate = models.ForeignKey(GstRate, on_delete=models.PROTECT, related_name='invoices')
    # Optional additional charge (one type + amount) added on top of taxable+GST.
    additional_charge_type = models.CharField(
        max_length=20, choices=AdditionalCharge.choices, blank=True, default='')
    additional_charge_amount = models.DecimalField(
        max_digits=15, decimal_places=2, default=0)
    # Always derived on save: taxable + GST amount + additional charge.
    invoice_value = models.DecimalField(max_digits=15, decimal_places=2)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='invoices')
    unit = models.ForeignKey(Unit, on_delete=models.PROTECT, related_name='invoices')
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name='invoices')
    mode = models.ForeignKey(InvoiceMode, on_delete=models.PROTECT, related_name='invoices')

    # --- Flow state (auto-managed as it moves) ---
    current_stage = models.ForeignKey(
        Stage, on_delete=models.PROTECT, related_name='current_invoices',
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.IN_PROGRESS,
    )
    # When the invoice arrived at its current stage — the basis for the live
    # "days at this stage" figure and the stuck-beyond-threshold alert.
    current_stage_entered_at = models.DateTimeField()
    # Flipped True the moment the invoice leaves Stage 1; blocks the entry
    # user from any further edits.
    is_locked = models.BooleanField(default=False)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='tracker_invoices_created',
    )
    created_at = models.DateTimeField(auto_now_add=True)   # == "Head Office In"
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tracker_invoice'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['current_stage', 'status']),
            models.Index(fields=['invoice_number']),
            models.Index(fields=['party_name']),
        ]

    @property
    def gst_amount(self):
        """GST value = taxable × rate%. Derived, not stored."""
        rate = self.gst_rate.rate if self.gst_rate_id else Decimal('0')
        taxable = self.taxable_value or Decimal('0')
        return (taxable * rate / Decimal('100')).quantize(Decimal('0.01'))

    def save(self, *args, **kwargs):
        # Keep invoice numbers clean so uniqueness isn't defeated by stray spaces.
        self.invoice_number = (self.invoice_number or '').strip()
        # Invoice value is always derived: taxable + GST amount + additional charge.
        taxable = self.taxable_value or Decimal('0')
        add = self.additional_charge_amount or Decimal('0')
        self.invoice_value = (taxable + self.gst_amount + add).quantize(Decimal('0.01'))
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.invoice_number} — {self.party_name}'


# ---------------------------------------------------------------------------
# Append-only handoff log — the single source of truth for every timeline.
# ---------------------------------------------------------------------------
class StageEvent(models.Model):
    class EventType(models.TextChoices):
        RECEIVE = 'RECEIVE', 'Received'
        ADVANCE = 'ADVANCE', 'Advanced'
        RETURN = 'RETURN', 'Returned'

    class ReceivingNote(models.TextChoices):
        ON_TIME = 'ON_TIME', 'On time'
        LATE = 'LATE', 'Late received (After 6 PM)'

    class HoldType(models.TextChoices):
        FULL = 'FULL', 'Full hold (work on it later)'
        PARTIAL = 'PARTIAL', 'Partial hold (portion of value)'

    invoice = models.ForeignKey(
        Invoice, on_delete=models.CASCADE, related_name='events',
    )
    stage = models.ForeignKey(Stage, on_delete=models.PROTECT, related_name='events')

    event_type = models.CharField(max_length=10, choices=EventType.choices)
    # Stage-specific disposition, validated against Stage.status_choices
    # (e.g. OK / HOLD / DEBIT / RETURN / APPROVED / REJECTED). Blank where the
    # stage has no status list.
    stage_status = models.CharField(max_length=30, blank=True, default='')
    # For a HOLD: whether it's a full hold (invoice stays) or a partial hold
    # (a portion of the value is withheld and the invoice advances).
    hold_type = models.CharField(
        max_length=10, choices=HoldType.choices, blank=True, default='')
    # Amount withheld (partial hold) or debited (DEBIT status).
    amount = models.DecimalField(
        max_digits=15, decimal_places=2, null=True, blank=True)
    receiving_note = models.CharField(
        max_length=10, choices=ReceivingNote.choices, blank=True, default='',
    )
    remarks = models.TextField(blank=True, default='')

    acted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='tracker_stage_events',
    )
    entered_at = models.DateTimeField()                 # arrival at this stage
    exited_at = models.DateTimeField(null=True, blank=True)   # set when it moves on
    days_spent = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tracker_stage_event'
        ordering = ['invoice_id', 'entered_at']
        indexes = [
            models.Index(fields=['invoice', 'stage']),
            models.Index(fields=['stage', 'event_type']),
        ]

    def __str__(self):
        return f'{self.invoice_id} @ {self.stage.code} [{self.event_type}]'


# ---------------------------------------------------------------------------
# Payment stage detail (1:1). Filled at the terminal Payment desk.
# ---------------------------------------------------------------------------
class PaymentDetail(models.Model):
    class Status(models.TextChoices):
        OPEN = 'OPEN', 'Open'
        PAID = 'PAID', 'Paid'

    invoice = models.OneToOneField(
        Invoice, on_delete=models.CASCADE, related_name='payment',
    )
    discount_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    tds_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    paid_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    open_balance = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)

    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='tracker_payments_updated',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tracker_payment_detail'

    def __str__(self):
        return f'Payment[{self.invoice_id}] {self.status}'


# ---------------------------------------------------------------------------
# Side modules — standalone registers mirroring the existing sheets.
# ---------------------------------------------------------------------------
class TransporterPayment(models.Model):
    class Status(models.TextChoices):
        OPEN = 'OPEN', 'Open'
        PAID = 'PAID', 'Paid'

    transport_bill_no = models.CharField(max_length=100)
    party_name = models.CharField(max_length=255)
    discount_pct = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    tds_pct = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    paid_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    open_balance = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='tracker_transporter_payments',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tracker_transporter_payment'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.transport_bill_no} — {self.party_name}'


class StuckAlert(models.Model):
    """A raised flag that an invoice has sat at a stage past its threshold.

    Keyed to a specific stage *visit* via `stage_entered_at`, so a bounce that
    re-enters the same stage later raises a fresh alert rather than reusing the
    old one. The periodic `scan_stuck_alerts` command creates these and
    resolves them once the invoice moves on.
    """
    invoice = models.ForeignKey(
        Invoice, on_delete=models.CASCADE, related_name='alerts',
    )
    stage = models.ForeignKey(Stage, on_delete=models.CASCADE, related_name='alerts')
    stage_entered_at = models.DateTimeField()
    days_stuck = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    threshold_days = models.PositiveIntegerField(default=0)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'tracker_stuck_alert'
        unique_together = ['invoice', 'stage', 'stage_entered_at']
        ordering = ['-days_stuck']
        indexes = [models.Index(fields=['is_active', 'stage'])]

    def __str__(self):
        return f'Stuck: {self.invoice_id} @ {self.stage.code} ({self.days_stuck}d)'


class CashVoucher(models.Model):
    class Status(models.TextChoices):
        OPEN = 'OPEN', 'Open'
        CLOSED = 'CLOSED', 'Closed'

    voucher_number = models.CharField(max_length=100)
    voucher_date = models.DateField()
    details = models.TextField(blank=True, default='')
    amount = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    unit = models.ForeignKey(
        Unit, on_delete=models.PROTECT, related_name='cash_vouchers',
        null=True, blank=True,
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='tracker_cash_vouchers',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'tracker_cash_voucher'
        ordering = ['-voucher_date']

    def __str__(self):
        return f'{self.voucher_number} — {self.amount}'
