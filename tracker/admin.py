from django.contrib import admin

from .models import (
    AlertNotification, Branch, CashVoucher, Category, GstRate, GstType, Invoice,
    InvoiceMode, PaymentDetail, Stage, StageEvent, StuckAlert, TransporterPayment,
    Unit, UserStageAccess,
)


@admin.register(Stage)
class StageAdmin(admin.ModelAdmin):
    list_display = ('order', 'name', 'code', 'requires_status', 'can_return',
                    'is_terminal', 'threshold_days', 'is_active')
    list_editable = ('threshold_days', 'is_active')
    ordering = ('order',)


@admin.register(UserStageAccess)
class UserStageAccessAdmin(admin.ModelAdmin):
    list_display = ('user', 'stage', 'is_active', 'assigned_by', 'assigned_at')
    list_filter = ('stage', 'is_active')
    search_fields = ('user__username',)
    autocomplete_fields = ('user', 'assigned_by')


class _LookupAdmin(admin.ModelAdmin):
    list_display = ('name', 'sort_order', 'is_active')
    list_editable = ('sort_order', 'is_active')


for _model in (Category, Unit, Branch, InvoiceMode, GstType):
    admin.site.register(_model, _LookupAdmin)


@admin.register(GstRate)
class GstRateAdmin(admin.ModelAdmin):
    list_display = ('label', 'rate', 'sort_order', 'is_active')
    list_editable = ('sort_order', 'is_active')


class StageEventInline(admin.TabularInline):
    model = StageEvent
    extra = 0
    readonly_fields = ('stage', 'event_type', 'stage_status', 'receiving_note',
                       'remarks', 'acted_by', 'entered_at', 'exited_at', 'days_spent')
    can_delete = False


class AlertNotificationInline(admin.TabularInline):
    """On the Invoice page: who was mailed about this invoice, and when."""
    model = AlertNotification
    extra = 0
    readonly_fields = ('stage', 'user', 'email', 'days_stuck', 'sent_at')
    can_delete = False
    ordering = ('-sent_at',)


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ('invoice_number', 'party_name', 'invoice_date',
                    'current_stage', 'status', 'is_locked', 'created_by', 'created_at')
    list_filter = ('current_stage', 'status', 'branch', 'unit', 'category')
    search_fields = ('invoice_number', 'party_name')
    inlines = [StageEventInline, AlertNotificationInline]
    readonly_fields = ('current_stage', 'current_stage_entered_at', 'is_locked',
                       'created_by', 'created_at', 'updated_at')


@admin.register(PaymentDetail)
class PaymentDetailAdmin(admin.ModelAdmin):
    list_display = ('invoice', 'discount_amount', 'tds_amount', 'paid_amount',
                    'open_balance', 'status', 'updated_at')


@admin.register(TransporterPayment)
class TransporterPaymentAdmin(admin.ModelAdmin):
    list_display = ('transport_bill_no', 'party_name', 'discount_pct', 'tds_pct',
                    'paid_amount', 'open_balance', 'status')
    search_fields = ('transport_bill_no', 'party_name')


@admin.register(StuckAlert)
class StuckAlertAdmin(admin.ModelAdmin):
    list_display = ('invoice', 'stage', 'days_stuck', 'threshold_days',
                    'is_active', 'created_at', 'resolved_at')
    list_filter = ('is_active', 'stage')
    search_fields = ('invoice__invoice_number', 'invoice__party_name')


@admin.register(AlertNotification)
class AlertNotificationAdmin(admin.ModelAdmin):
    """Audit trail: which user was mailed about which invoice, and when."""
    list_display = ('sent_at', 'user', 'email', 'invoice', 'stage', 'days_stuck')
    list_filter = ('stage', 'sent_at')
    search_fields = ('invoice__invoice_number', 'invoice__party_name',
                     'user__username', 'email')
    date_hierarchy = 'sent_at'
    autocomplete_fields = ('invoice', 'user', 'alert')
    readonly_fields = ('alert', 'invoice', 'stage', 'user', 'email',
                       'days_stuck', 'sent_at')


@admin.register(CashVoucher)
class CashVoucherAdmin(admin.ModelAdmin):
    list_display = ('voucher_number', 'voucher_date', 'amount', 'unit', 'status')
    search_fields = ('voucher_number',)
