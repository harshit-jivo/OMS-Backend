from django.contrib import admin

from .models import (
    BankDeposit,
    BankDepositLine,
    CashDenomination,
    CollectionPerson,
    PaymentAllocation,
    PaymentMethodEntry,
    PaymentReceipt,
    PaymentStatusHistory,
    SapCallLog,
    SapCompanyMap,
)


@admin.register(SapCompanyMap)
class SapCompanyMapAdmin(admin.ModelAdmin):
    list_display = ('company', 'display_name', 'company_db', 'hana_schema',
                    'default_bpl_id', 'is_active')
    list_editable = ('is_active',)


@admin.register(CollectionPerson)
class CollectionPersonAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'company', 'phone', 'is_active')
    list_filter = ('company', 'is_active')
    search_fields = ('name', 'code')


class PaymentMethodEntryInline(admin.TabularInline):
    model = PaymentMethodEntry
    extra = 0


class PaymentAllocationInline(admin.TabularInline):
    model = PaymentAllocation
    extra = 0


@admin.register(PaymentReceipt)
class PaymentReceiptAdmin(admin.ModelAdmin):
    list_display = ('receipt_no', 'company', 'card_name', 'payment_date',
                    'total_amount', 'status', 'sap_doc_num')
    list_filter = ('company', 'status', 'is_advance', 'payment_date')
    search_fields = ('receipt_no', 'card_code', 'card_name')
    readonly_fields = ('receipt_no', 'company_db', 'sap_doc_entry',
                       'sap_doc_num', 'sap_posted_at', 'sap_response')
    inlines = [PaymentMethodEntryInline, PaymentAllocationInline]

    def has_delete_permission(self, request, obj=None):
        return False        # financial records are never deleted from the admin


@admin.register(CashDenomination)
class CashDenominationAdmin(admin.ModelAdmin):
    list_display = ('entry', 'denomination', 'quantity', 'line_total')


class BankDepositLineInline(admin.TabularInline):
    model = BankDepositLine
    extra = 0


@admin.register(BankDeposit)
class BankDepositAdmin(admin.ModelAdmin):
    list_display = ('deposit_no', 'company', 'deposit_date', 'bank_display_name',
                    'collected_amount', 'deposit_amount', 'status', 'sap_doc_num')
    list_filter = ('company', 'status', 'deposit_type', 'deposit_date')
    search_fields = ('deposit_no', 'slip_number')
    readonly_fields = ('deposit_no', 'company_db', 'collected_amount',
                       'sap_doc_entry', 'sap_doc_num', 'sap_posted_at',
                       'sap_response')
    inlines = [BankDepositLineInline]

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SapCallLog)
class SapCallLogAdmin(admin.ModelAdmin):
    """Read-only forensic log of every Service Layer call."""

    list_display = ('created_at', 'endpoint', 'company_db',
                    'status', 'http_status')
    list_filter = ('status', 'company_db', 'created_at')
    search_fields = ('endpoint', 'error_message')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(PaymentStatusHistory)
class PaymentStatusHistoryAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'content_type', 'object_id', 'action',
                    'from_status', 'to_status', 'changed_by_username')
    # Filtering on `action` rather than the dropped `actor_kind`: it is the
    # event identity, so it answers "show me every SAP failure" directly.
    list_filter = ('action', 'to_status', 'created_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


