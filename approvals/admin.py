from django.contrib import admin

from .models import (
    ApprovalAction,
    ApprovalLevel,
    ApprovalLevelApprover,
    ApprovalRequest,
    ApprovalWorkflow,
)


class ApprovalLevelInline(admin.TabularInline):
    model = ApprovalLevel
    extra = 0
    fields = ('sequence', 'name', 'role', 'min_approvals', 'is_active')


@admin.register(ApprovalWorkflow)
class ApprovalWorkflowAdmin(admin.ModelAdmin):
    list_display = ('code', 'name', 'document_type', 'company', 'is_active')
    list_filter = ('document_type', 'company', 'is_active')
    search_fields = ('code', 'name')
    inlines = [ApprovalLevelInline]


class ApprovalLevelApproverInline(admin.TabularInline):
    model = ApprovalLevelApprover
    extra = 0
    autocomplete_fields = ('user',)
    fields = ('user', 'company', 'is_active')


@admin.register(ApprovalLevel)
class ApprovalLevelAdmin(admin.ModelAdmin):
    list_display = ('workflow', 'sequence', 'name', 'role', 'min_approvals', 'is_active')
    list_filter = ('workflow', 'is_active')
    inlines = [ApprovalLevelApproverInline]


class ApprovalActionInline(admin.TabularInline):
    """Read-only: the action log is append-only and must never be edited here."""

    model = ApprovalAction
    extra = 0
    can_delete = False
    readonly_fields = ('sequence', 'round_number', 'level', 'level_name', 'action',
                       'remarks', 'approver_username', 'approver_role',
                       'ip_address', 'acted_at')
    fields = readonly_fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ApprovalRequest)
class ApprovalRequestAdmin(admin.ModelAdmin):
    list_display = ('document_number', 'workflow', 'company', 'amount',
                    'status', 'level_label', 'submitted_by', 'created_at')
    list_filter = ('status', 'company', 'workflow__document_type')
    search_fields = ('document_number',)
    readonly_fields = ('content_type', 'object_id', 'total_levels', 'round_number',
                       'submitted_at', 'level_entered_at', 'decided_at')
    inlines = [ApprovalActionInline]

    def has_delete_permission(self, request, obj=None):
        # Financial approval history is never deleted through the admin.
        return False


@admin.register(ApprovalAction)
class ApprovalActionAdmin(admin.ModelAdmin):
    """Fully read-only — append-only log."""

    list_display = ('request', 'sequence', 'round_number', 'level',
                    'action', 'approver_username', 'acted_at')
    list_filter = ('action', 'acted_at')
    search_fields = ('approver_username', 'remarks')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
