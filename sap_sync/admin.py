from django.contrib import admin
from .models import Product, Party, PartyAddress, Branch, SyncLog, SyncSchedule


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ['item_code', 'item_name', 'category', 'brand', 'is_deleted', 'synced_at']
    list_filter = ['category', 'brand', 'is_deleted']
    search_fields = ['item_code', 'item_name', 'brand']
    readonly_fields = ['synced_at', 'created_at']


@admin.register(Party)
class PartyAdmin(admin.ModelAdmin):
    list_display = ['card_code', 'card_name', 'category', 'state', 'main_group', 'synced_at']
    list_filter = ['category', 'card_type', 'state', 'main_group']
    search_fields = ['card_code', 'card_name']
    readonly_fields = ['synced_at']  # ✅ FIXED - removed 'created_at' (doesn't exist)


@admin.register(PartyAddress)
class PartyAddressAdmin(admin.ModelAdmin):
    list_display = ['card_code', 'address_name', 'address_type', 'category', 'state', 'city', 'gst_number', 'synced_at']  # ✅ FIXED - address_id → address_name
    list_filter = ['category', 'address_type', 'state']
    search_fields = ['card_code', 'address_name', 'gst_number', 'full_address']
    readonly_fields = ['synced_at']  # ✅ FIXED - removed 'created_at' (doesn't exist)


@admin.register(Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ['bpl_id', 'bpl_name', 'category', 'is_active', 'updated_at']
    list_filter = ['category', 'is_active']
    search_fields = ['bpl_id', 'bpl_name']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(SyncLog)
class SyncLogAdmin(admin.ModelAdmin):
    list_display = ['sync_type', 'status', 'records_processed', 'records_created', 'records_updated', 'triggered_by', 'started_at', 'completed_at']
    list_filter = ['sync_type', 'status', 'triggered_by']
    readonly_fields = ['sync_type', 'status', 'records_processed', 'records_created', 'records_updated', 'error_message', 'started_at', 'completed_at', 'triggered_by']
    ordering = ['-started_at']


@admin.register(SyncSchedule)
class SyncScheduleAdmin(admin.ModelAdmin):
    list_display = ['name', 'sync_type', 'frequency', 'is_active', 'last_run', 'next_run']
    list_filter = ['sync_type', 'frequency', 'is_active']
    list_editable = ['is_active']
    readonly_fields = ['last_run', 'next_run', 'created_at', 'updated_at']