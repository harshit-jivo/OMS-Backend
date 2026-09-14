"""Django Admin for workflow configuration.

Only the five configuration models are registered. There is no runtime
here: tasks, actions and flow state belong to the business module, which
registers its own. `WorkflowQuery.validated_at` stays read-only
history and must never be edited or deleted through the admin.
"""
from django.contrib import admin

from workflow.models import (
    Workflow,
    WorkflowModule,
    WorkflowQuery,
    WorkflowStage,
    WorkflowUserReplacement,
)
from workflow.services import conditions


class WorkflowStageInline(admin.TabularInline):
    model = WorkflowStage
    extra = 1
    fields = ('sequence', 'name', 'user')
    ordering = ('sequence',)


class WorkflowQueryInline(admin.TabularInline):
    model = WorkflowQuery
    extra = 1
    fields = ('name', 'company',
              'query_text', 'validated_at', 'validation_error')
    readonly_fields = ('validated_at', 'validation_error')


@admin.register(WorkflowModule)
class WorkflowModuleAdmin(admin.ModelAdmin):
    list_display = ('id', 'code', 'name', 'created_at', 'updated_at')
    search_fields = ('code', 'name')
    ordering = ('id',)


@admin.register(Workflow)
class WorkflowAdmin(admin.ModelAdmin):
    # `applies_to` is shown because a workflow's applicability is the first
    # thing an operator needs when diagnosing an ambiguous selection.
    list_display = ('code', 'name', 'module', 'applies_to', 'stage_count',
                    'query_count')
    list_filter = ('module', 'company')
    search_fields = ('code', 'name')
    inlines = [WorkflowStageInline, WorkflowQueryInline]

    @admin.display(description='Applies to')
    def applies_to(self, obj):
        return 'ALL companies' if obj.company == 'ALL' else obj.company

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('module')
                .prefetch_related('stages', 'queries'))

    @admin.display(description='Stages')
    def stage_count(self, obj):
        return obj.stages.count()

    @admin.display(description='Queries')
    def query_count(self, obj):
        return obj.queries.count()


@admin.register(WorkflowQuery)
class WorkflowQueryAdmin(admin.ModelAdmin):
    list_display = ('name', 'workflow', 'company',
                    'is_validated', 'scope_ok')
    list_filter = ('company', 'workflow__module')
    search_fields = ('name', 'query_text')
    readonly_fields = ('validated_at', 'validation_error')
    actions = ['revalidate']

    @admin.display(boolean=True, description='Scope OK')
    def scope_ok(self, obj):
        """False when the query's scope contradicts its workflow's."""
        return obj.scope_conflict() is None

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('workflow', 'workflow__module'))

    @admin.display(boolean=True, description='Validated')
    def is_validated(self, obj):
        return obj.validated_at is not None

    def save_model(self, request, obj, form, change):
        # Same gate as the API: a query saved here is validated before it can
        # take part in selection. `validated_at` is never hand-set.
        super().save_model(request, obj, form, change)
        conditions.validate_and_stamp(obj)
        conflict = obj.scope_conflict()
        if conflict:
            # Saved, but it can never match — say so loudly rather than let it
            # sit in the configuration looking active.
            self.message_user(request, conflict, level='WARNING')

    @admin.action(description='Re-run validation on selected queries')
    def revalidate(self, request, queryset):
        ok_count = 0
        for query in queryset.select_related('workflow', 'workflow__module'):
            if not conditions.validate_and_stamp(query):
                ok_count += 1
        self.message_user(
            request,
            f'{ok_count} of {queryset.count()} queries validated successfully.',
        )


@admin.register(WorkflowStage)
class WorkflowStageAdmin(admin.ModelAdmin):
    list_display = ('workflow', 'sequence', 'name', 'user')
    list_filter = ('workflow__module', 'workflow')
    search_fields = ('name',)
    ordering = ('workflow', 'sequence')

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('workflow', 'user')


@admin.register(WorkflowUserReplacement)
class WorkflowUserReplacementAdmin(admin.ModelAdmin):
    list_display = ('old_user', 'new_user', 'start_date', 'end_date', 'reason')
    list_filter = ('start_date',)
    search_fields = ('old_user__username', 'new_user__username', 'reason')
    autocomplete_fields = ()

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .select_related('old_user', 'new_user'))


# No admin for tasks, actions or a TestFlow harness: those models are gone.
# A module registers its own approval runtime in its own admin.
