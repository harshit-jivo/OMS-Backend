from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'username', 'page', 'action', 'record', 'field', 'old_value', 'new_value']
    list_filter = ['page', 'action', 'created_at']
    search_fields = ['username', 'page', 'record', 'field', 'old_value', 'new_value']
    date_hierarchy = 'created_at'
    ordering = ['-created_at']
    readonly_fields = ['user', 'username', 'page', 'action', 'record', 'field', 'old_value', 'new_value', 'created_at']

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        return  # read-only
