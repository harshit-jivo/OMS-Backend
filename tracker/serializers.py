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
            'discount_amount', 'tds_amount', 'paid_amount',
            'open_balance', 'status', 'updated_at',
        ]
        read_only_fields = ['updated_at']


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

    def validate_invoice_number(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('Invoice number is required.')
        qs = Invoice.objects.filter(invoice_number__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError('This invoice number already exists.')
        return value

    class Meta:
        model = Invoice
        fields = [
            'invoice_date', 'party_name', 'party_code', 'party_gstin',
            'invoice_number', 'taxable_value', 'gst_type', 'gst_rate',
            'additional_charge_type', 'additional_charge_amount',
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

    days_at_stage = serializers.SerializerMethodField()
    is_overdue = serializers.SerializerMethodField()
    editable = serializers.SerializerMethodField()

    class Meta:
        model = Invoice
        fields = [
            'id', 'invoice_date', 'party_name', 'party_code', 'party_gstin',
            'invoice_number',
            'taxable_value', 'gst_type', 'gst_type_name', 'gst_rate',
            'gst_rate_label', 'gst_amount', 'additional_charge_type',
            'additional_charge_type_display', 'additional_charge_amount',
            'invoice_value', 'category', 'category_name',
            'unit', 'unit_name', 'branch', 'branch_name', 'mode', 'mode_name',
            'current_stage', 'current_stage_code', 'current_stage_name',
            'status', 'current_stage_entered_at', 'is_locked',
            'days_at_stage', 'is_overdue', 'editable',
            'created_by', 'created_by_name', 'created_at', 'updated_at',
        ]

    def get_days_at_stage(self, obj):
        return services.days_at_stage(obj)

    def get_is_overdue(self, obj):
        return services.is_overdue(obj)

    def get_editable(self, obj):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if not user:
            return False
        return (
            not obj.is_locked
            and obj.current_stage.code == 'entry'
            and (obj.created_by_id == user.id or user.is_superuser)
        )


class StuckAlertSerializer(serializers.ModelSerializer):
    invoice_number = serializers.CharField(source='invoice.invoice_number', read_only=True)
    party_name = serializers.CharField(source='invoice.party_name', read_only=True)
    invoice_value = serializers.DecimalField(
        source='invoice.invoice_value', max_digits=15, decimal_places=2, read_only=True)
    stage_name = serializers.CharField(source='stage.name', read_only=True)
    stage_code = serializers.CharField(source='stage.code', read_only=True)
    over_by = serializers.SerializerMethodField()

    class Meta:
        model = StuckAlert
        fields = [
            'id', 'invoice', 'invoice_number', 'party_name', 'invoice_value',
            'stage', 'stage_name', 'stage_code', 'stage_entered_at',
            'days_stuck', 'threshold_days', 'over_by', 'is_active',
            'created_at', 'updated_at',
        ]

    def get_over_by(self, obj):
        return float(obj.days_stuck) - obj.threshold_days


class InvoiceDetailSerializer(InvoiceListSerializer):
    events = StageEventSerializer(many=True, read_only=True)
    payment = PaymentDetailSerializer(read_only=True)

    class Meta(InvoiceListSerializer.Meta):
        fields = InvoiceListSerializer.Meta.fields + ['events', 'payment']
