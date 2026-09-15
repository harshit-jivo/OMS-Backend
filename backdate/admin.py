"""Read-mostly admin for BKDT.

History is append-only, so `BackDateActionLog` is registered without add,
change or delete: the admin is for LOOKING at what happened, and an editable
audit trail is not one.

Nothing here shows a stage NAME from a BKDT column, because no BKDT column
holds one — it is read through the stage relation, so a renamed stage reads
correctly in the admin too.
"""
from django.contrib import admin

from .models import BackDate, BackDateActionLog, BackDateFlow


@admin.register(BackDate)
class BackDateAdmin(admin.ModelAdmin):
    list_display = ('id', 'company', 'sap_username', 'document_type_name',
                    'from_date', 'to_date', 'time_limit', 'action',
                    'created_by', 'created_at')
    list_filter = ('company', 'action', 'created_at')
    search_fields = ('sap_username', 'document_type_name',
                     'created_by__username')
    date_hierarchy = 'created_at'
    readonly_fields = ('created_at', 'updated_at')
    autocomplete_fields = ('created_by',)


@admin.register(BackDateFlow)
class BackDateFlowAdmin(admin.ModelAdmin):
    list_display = ('id', 'backdate', 'status', 'current_stage',
                    'current_user', 'total_stage', 'hana_status', 'updated_at')
    list_filter = ('status', 'hana_status')
    search_fields = ('backdate__sap_username',)
    readonly_fields = ('created_at', 'updated_at')
    raw_id_fields = ('backdate', 'workflow', 'current_stage', 'current_user')


@admin.register(BackDateActionLog)
class BackDateActionLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'backdate', 'action', 'stage', 'acted_by',
                    'acted_at')
    list_filter = ('action', 'acted_at')
    search_fields = ('remarks', 'acted_by__username')
    date_hierarchy = 'acted_at'
    raw_id_fields = ('backdate', 'stage', 'acted_by')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
