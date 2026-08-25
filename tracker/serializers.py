from rest_framework import serializers

from . import services
from .models import (
    Branch, Category, GstRate, GstType, Invoice, InvoiceMode, PaymentDetail,
    Stage, StageEvent, StuckAlert, Unit,
)


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------
class _LookupSerializer(serializers.ModelSerializer):
    class Meta:
        fields = ['id', 'name', 'is_active', 'sort_order']


class CategorySerializer(_LookupSerializer):
    class Meta(_LookupSerializer.Meta):
        model = Category


class UnitSerializer(_LookupSerializer):
    class Meta(_LookupSerializer.Meta):
        model = Unit


class BranchSerializer(_LookupSerializer):
    class Meta(_LookupSerializer.Meta):
        model = Branch


class InvoiceModeSerializer(_LookupSerializer):
    class Meta(_LookupSerializer.Meta):
        model = InvoiceMode


class GstTypeSerializer(_LookupSerializer):
    class Meta(_LookupSerializer.Meta):
        model = GstType


class GstRateSerializer(serializers.ModelSerializer):
    class Meta:
        model = GstRate
        fields = ['id', 'label', 'rate', 'is_active', 'sort_order']


class StageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Stage
        fields = [
            'id', 'code', 'name', 'order', 'threshold_days',
            'status_choices', 'requires_status', 'can_return',
            'is_terminal', 'is_active',
        ]


# ---------------------------------------------------------------------------
# Stage events / payment
# ---------------------------------------------------------------------------
class StageEventSerializer(serializers.ModelSerializer):
    stage_name = serializers.CharField(source='stage.name', read_only=True)
    stage_code = serializers.CharField(source='stage.code', read_only=True)
    acted_by_name = serializers.CharField(source='acted_by.username', read_only=True, default=None)

    class Meta:
        model = StageEvent
        fields = [
            'id', 'stage', 'stage_name', 'stage_code', 'event_type',
            'stage_status', 'hold_type', 'amount', 'receiving_note', 'remarks',
            'acted_by', 'acted_by_name', 'entered_at', 'exited_at', 'days_spent',
        ]


class PaymentDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentDetail
        fields = [
            'discount_pct', 'tds_pct', 'hold_added_back',
            'discount_amount', 'tds_amount',
            'paid_amount', 'open_balance', 'status', 'updated_at',
        ]
        # Amounts/balance/status are always derived server-side from the
        # percentages, the hold-release flag and the paid amount; only those
        # inputs are writable.
        read_only_fields = [
            'discount_amount', 'tds_amount', 'open_balance', 'status', 'updated_at',
        ]


# ---------------------------------------------------------------------------
# Invoice — write (entry stage) and read
# ---------------------------------------------------------------------------
class InvoiceWriteSerializer(serializers.ModelSerializer):
    """Only the entry-stage fields are writable. Flow state is engine-managed.
    `invoice_value` is NOT accepted — it is always derived on the server as
    taxable + GST amount + additional charge."""
    # Declared explicitly so our own (case-insensitive) uniqueness check runs
    # instead of the default validator.
    invoice_number = serializers.CharField(max_length=100)

    def validate_invoice_date(self, value):
        # An invoice can't be dated in the future.
        from django.utils import timezone
        if value and value > timezone.localdate():
            raise serializers.ValidationError('Invoice date cannot be in the future.')
        return value

    def validate_invoice_number(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('Invoice number is required.')
        return value

    def validate(self, attrs):
        """Duplicate check, scoped to the vendor.

        Object-level rather than field-level because the vendor is part of the
        key, and a field validator only sees its own value. Mirrors the
        `uniq_live_vendor_invoice_number` partial index exactly:

          * LIVE rows only — a soft-deleted invoice releases its number
            (deletion is capped at stage 6, so it was never saved in SAP or
            paid, and re-entry is how a bad entry gets corrected);
          * vendor key = party_code, falling back to party_name when the vendor
            was hand-typed rather than picked from SAP;
          * case-insensitive on both parts.
        """
        attrs = super().validate(attrs)

        def field(name):
            # On PATCH the field may be absent — fall back to the stored value.
            if name in attrs:
                return (attrs.get(name) or '').strip()
            return (getattr(self.instance, name, '') or '').strip() if self.instance else ''

        number, code, name = field('invoice_number'), field('party_code'), field('party_name')
        if not number:
            return attrs

        qs = Invoice.objects.filter(invoice_number__iexact=number)
        # Same fallback the index uses, so the two can never disagree.
        if code:
            qs = qs.filter(party_code__iexact=code)
        else:
            qs = qs.filter(party_code='', party_name__iexact=name)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError({
                'invoice_number': f'Invoice {number} already exists for '
                                  f'{name or code}.',
            })
        return attrs

    class Meta:
        model = Invoice
        fields = [
            'invoice_date', 'effective_month', 'party_name', 'party_code',
            'party_gstin', 'invoice_number', 'taxable_value', 'gst_type',
            'gst_rate', 'additional_charge_type', 'additional_charge_amount',
            'category', 'unit', 'branch', 'mode',
        ]


class InvoiceListSerializer(serializers.ModelSerializer):
    current_stage_code = serializers.CharField(source='current_stage.code', read_only=True)
    current_stage_name = serializers.CharField(source='current_stage.name', read_only=True)
    gst_type_name = serializers.CharField(source='gst_type.name', read_only=True)
    gst_rate_label = serializers.CharField(source='gst_rate.label', read_only=True)
    category_name = serializers.CharField(source='category.name', read_only=True)
    unit_name = serializers.CharField(source='unit.name', read_only=True)
    branch_name = serializers.CharField(source='branch.name', read_only=True)
    mode_name = serializers.CharField(source='mode.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    additional_charge_type_display = serializers.CharField(
        source='get_additional_charge_type_display', read_only=True)
    gst_amount = serializers.DecimalField(max_digits=15, decimal_places=2, read_only=True)
    net_invoice_value = serializers.DecimalField(max_digits=15, decimal_places=2, read_only=True)

    days_at_stage = serializers.SerializerMethodField()
    is_overdue = serializers.SerializerMethodField()
    editable = serializers.SerializerMethodField()
    # Lightweight payment summary so the queue can flag partial payments and show
    # balances without fetching each invoice's full detail.
    payment_status = serializers.SerializerMethodField()
    paid_amount = serializers.SerializerMethodField()
    open_balance = serializers.SerializerMethodField()
    is_partially_paid = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            'id', 'invoice_date', 'effective_month', 'party_name', 'party_code',
            'party_gstin', 'invoice_number',
            'taxable_value', 'gst_type', 'gst_type_name', 'gst_rate',
            'gst_rate_label', 'gst_amount', 'additional_charge_type',
            'additional_charge_type_display', 'additional_charge_amount',
            'invoice_value', 'debit_amount', 'hold_amount', 'net_invoice_value',
            'category', 'category_name',
            'unit', 'unit_name', 'branch', 'branch_name', 'mode', 'mode_name',
            'current_stage', 'current_stage_code', 'current_stage_name',
            'status', 'current_stage_entered_at', 'is_locked', 'rejection_pending',
            'days_at_stage', 'is_overdue', 'editable',
            'payment_status', 'paid_amount', 'open_balance', 'is_partially_paid',
            'created_by', 'created_by_name', 'created_at', 'updated_at',
        ]

    def _payment(self, obj):
        # Reverse one-to-one: a missing row raises DoesNotExist, not AttributeError.
        try:
            return obj.payment
        except PaymentDetail.DoesNotExist:
            return None

    def get_payment_status(self, obj):
        p = self._payment(obj)
        return p.status if p else None

    def get_paid_amount(self, obj):
        p = self._payment(obj)
        return str(p.paid_amount) if p else None

    def get_open_balance(self, obj):
        p = self._payment(obj)
        return str(p.open_balance) if p else None

    def get_is_partially_paid(self, obj):
        """OPEN with something already paid but a balance remaining."""
        p = self._payment(obj)
        return bool(p and p.status == PaymentDetail.Status.OPEN
                    and p.paid_amount and p.open_balance and p.open_balance > 0)

    def get_days_at_stage(self, obj):
        return services.days_at_stage(obj)

    def get_is_overdue(self, obj):
        return services.is_overdue(obj)

    def get_editable(self, obj):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if not user:
            return False
        # Shared head-office / entry desk: any entry-desk user can edit an
        # unlocked invoice sitting at the entry stage.
        from .permissions import PAGE_ENTRY, tracker_pages_for
        return (
            not obj.is_locked
            and obj.current_stage.code == 'entry'
            and (user.is_superuser or PAGE_ENTRY in tracker_pages_for(user))
        )


class StuckAlertSerializer(serializers.ModelSerializer):
    invoice_number = serializers.CharField(source='invoice.invoice_number', read_only=True)
    party_name = serializers.CharField(source='invoice.party_name', read_only=True)
    invoice_value = serializers.DecimalField(
        source='invoice.invoice_value', max_digits=15, decimal_places=2, read_only=True)
    stage_name = serializers.CharField(source='stage.name', read_only=True)
    stage_code = serializers.CharField(source='stage.code', read_only=True)
    over_by = serializers.SerializerMethodField()
    notified = serializers.SerializerMethodField()

    class Meta:
        model = StuckAlert
        fields = [
            'id', 'invoice', 'invoice_number', 'party_name', 'invoice_value',
            'stage', 'stage_name', 'stage_code', 'stage_entered_at',
            'days_stuck', 'threshold_days', 'over_by', 'is_active',
            'last_notified_at', 'notified',
            'created_at', 'updated_at',
        ]

    def get_over_by(self, obj):
        return float(obj.days_stuck) - obj.threshold_days

    def get_notified(self, obj):
        """Distinct users mailed about this alert, each with their latest send."""
        latest = {}
        for n in obj.notifications.select_related('user').all():
            name = (getattr(n.user, 'name', '') or getattr(n.user, 'username', '')
                    or n.email) if n.user_id else n.email
            cur = latest.get(n.user_id)
            if cur is None or n.sent_at > cur['sent_at']:
                latest[n.user_id] = {'user': name, 'email': n.email, 'sent_at': n.sent_at}
        return sorted(latest.values(), key=lambda x: x['sent_at'], reverse=True)


class InvoiceDetailSerializer(InvoiceListSerializer):
    events = StageEventSerializer(many=True, read_only=True)
    payment = PaymentDetailSerializer(read_only=True)

    class Meta(InvoiceListSerializer.Meta):
        fields = InvoiceListSerializer.Meta.fields + ['events', 'payment']
