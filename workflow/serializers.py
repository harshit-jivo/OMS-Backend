"""Serializers for workflow configuration and runtime reads.

Configuration writes run the query validator on save (`WorkflowQuerySerializer`),
so a query cannot be stored as usable without passing it — `validated_at` is
never settable from the API.
"""
from rest_framework import serializers

from workflow.models import (
    CompanyScope,
    TestDocument,
    TestDocumentLog,
    TestFlow,
    Workflow,
    WorkflowAction,
    WorkflowModule,
    WorkflowQuery,
    WorkflowStage,
    WorkflowTask,
    WorkflowUserReplacement,
)
from workflow.services import conditions


class WorkflowModuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowModule
        fields = ['id', 'code', 'name', 'business_table',
                  'business_key_column', 'flow_table', 'flow_model',
                  'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']

    def validate_code(self, value):
        # The DB CHECK enforces this too; validating here turns a 500 into a
        # field error.
        if value != value.upper():
            raise serializers.ValidationError('Module code must be UPPERCASE.')
        return value


class _CompanyScopeMixin:
    """Reject the contradictory scope pair as a field error, not a 500.

    The database CHECK (`*_company_scope_consistent`) is the guarantee; this
    turns the same rule into a readable 400 so a client never has to parse an
    IntegrityError.
    """

    def _validate_company_scope(self, attrs):
        scope = attrs.get('company_scope') or getattr(
            self.instance, 'company_scope', None)
        # `company` may legitimately be set to None, so distinguish "absent"
        # from "explicitly null".
        if 'company' in attrs:
            company = attrs['company']
        else:
            company = getattr(self.instance, 'company', None)

        if scope == CompanyScope.ALL and company:
            raise serializers.ValidationError({
                'company': 'Leave company empty when company_scope is ALL. '
                           'One ALL row already applies to every company.'
            })
        if scope == CompanyScope.SPECIFIC and not company:
            raise serializers.ValidationError({
                'company': 'A company is required when company_scope is '
                           'SPECIFIC.'
            })
        return attrs


class WorkflowStageSerializer(serializers.ModelSerializer):
    user_username = serializers.CharField(source='user.username',
                                          read_only=True)

    class Meta:
        model = WorkflowStage
        fields = ['id', 'workflow', 'name', 'sequence', 'user',
                  'user_username', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']

    def validate_sequence(self, value):
        if value < 1:
            raise serializers.ValidationError('sequence starts at 1.')
        return value


class WorkflowQuerySerializer(_CompanyScopeMixin, serializers.ModelSerializer):
    """Validation runs on save; `validated_at` is read-only by design."""

    validation_problems = serializers.SerializerMethodField()

    class Meta:
        model = WorkflowQuery
        fields = ['id', 'workflow', 'name', 'query_text', 'type',
                  'company_scope', 'company',
                  'key_column', 'validated_at', 'validation_error',
                  'validation_problems', 'created_at', 'updated_at']
        # A client must never be able to declare its own SQL validated.
        read_only_fields = ['validated_at', 'validation_error',
                            'created_at', 'updated_at']

    def get_validation_problems(self, obj):
        return obj.validation_error.split('\n') if obj.validation_error else []

    def validate(self, attrs):
        attrs = self._validate_company_scope(attrs)

        # A query may only NARROW its workflow's scope. Checked here because a
        # CHECK constraint cannot read the parent row.
        workflow = attrs.get('workflow') or getattr(
            self.instance, 'workflow', None)

        # --- SQL validated BEFORE any save --------------------------------
        # The database CHECK `workflow_query_select_only` rejects non-SELECT
        # text, but it fires during INSERT, so a DML query used to surface as
        # an unhandled IntegrityError -> HTTP 500. Validating here turns that
        # into a clean 400 and guarantees no row is written.
        #
        # This calls the ONE centralized validator; it does not re-implement
        # any rule, and the CHECK constraint stays exactly as it is.
        query_text = (attrs.get('query_text')
                      if 'query_text' in attrs
                      else getattr(self.instance, 'query_text', ''))
        key_column = (attrs.get('key_column')
                      if 'key_column' in attrs
                      else getattr(self.instance, 'key_column', ''))
        if workflow is not None and query_text:
            problems = conditions.check_query(workflow, query_text, key_column)
            if problems:
                raise serializers.ValidationError({'query_text': problems})
        scope = attrs.get('company_scope') or getattr(
            self.instance, 'company_scope', None)
        company = (attrs['company'] if 'company' in attrs
                   else getattr(self.instance, 'company', None))
        if (workflow is not None
                and workflow.company_scope == CompanyScope.SPECIFIC
                and scope == CompanyScope.SPECIFIC
                and company != workflow.company):
            raise serializers.ValidationError({
                'company': (
                    f'This query is scoped to {company}, but workflow '
                    f'"{workflow.code}" applies only to {workflow.company}. '
                    f'The query could never match. Use ALL or '
                    f'{workflow.company}.'
                )
            })
        return attrs

    def create(self, validated_data):
        query = super().create(validated_data)
        conditions.validate_and_stamp(query)
        query.refresh_from_db()
        return query

    def update(self, instance, validated_data):
        query = super().update(instance, validated_data)
        conditions.validate_and_stamp(query)
        query.refresh_from_db()
        return query


class WorkflowSerializer(_CompanyScopeMixin, serializers.ModelSerializer):
    module_code = serializers.CharField(source='module.code', read_only=True)
    stages = WorkflowStageSerializer(many=True, read_only=True)
    queries = WorkflowQuerySerializer(many=True, read_only=True)

    class Meta:
        model = Workflow
        fields = ['id', 'module', 'module_code', 'code', 'name',
                  'company_scope', 'company',
                  'stages', 'queries', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']

    def validate(self, attrs):
        return self._validate_company_scope(attrs)


class WorkflowUserReplacementSerializer(serializers.ModelSerializer):
    old_username = serializers.CharField(source='old_user.username',
                                         read_only=True)
    new_username = serializers.CharField(source='new_user.username',
                                         read_only=True)

    class Meta:
        model = WorkflowUserReplacement
        fields = ['id', 'old_user', 'old_username', 'new_user', 'new_username',
                  'reason', 'start_date', 'end_date',
                  'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']

    def validate(self, attrs):
        old = attrs.get('old_user') or getattr(self.instance, 'old_user', None)
        new = attrs.get('new_user') or getattr(self.instance, 'new_user', None)
        start = attrs.get('start_date') or getattr(self.instance, 'start_date', None)
        end = attrs.get('end_date') or getattr(self.instance, 'end_date', None)
        if old and new and old == new:
            raise serializers.ValidationError(
                'old_user and new_user must be different.')
        if start and end and end < start:
            raise serializers.ValidationError(
                'end_date must not be before start_date.')
        # Overlap is enforced by the database exclusion constraint; the view
        # turns that IntegrityError into a readable message rather than
        # duplicating the check here (and racing it).
        return attrs


class WorkflowTaskSerializer(serializers.ModelSerializer):
    module_code = serializers.CharField(source='module.code', read_only=True)
    stage_name = serializers.CharField(source='stage.name', read_only=True)
    workflow_code = serializers.CharField(source='stage.workflow.code',
                                          read_only=True)
    stage_username = serializers.CharField(source='stage_user.username',
                                           read_only=True)

    class Meta:
        model = WorkflowTask
        fields = ['id', 'module', 'module_code', 'flow_id', 'stage',
                  'stage_name', 'workflow_code', 'sequence',
                  'stage_user', 'stage_username', 'status',
                  'created_at', 'updated_at']


class WorkflowActionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowAction
        fields = ['id', 'module', 'flow_id', 'sequence', 'stage', 'stage_name',
                  'action', 'acted_by', 'acted_by_username', 'on_behalf_of',
                  'remarks', 'ip_address', 'acted_at']


# ---------------------------------------------------------------------------
# TestFlow harness
# ---------------------------------------------------------------------------

class TestDocumentLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = TestDocumentLog
        fields = ['id', 'sequence', 'event', 'remarks', 'created_at']


class TestFlowSerializer(serializers.ModelSerializer):
    workflow_code = serializers.CharField(source='workflow.code',
                                          read_only=True)
    matched_query_name = serializers.CharField(source='matched_query.name',
                                               read_only=True)
    current_stage_name = serializers.CharField(source='current_stage.name',
                                               read_only=True)

    class Meta:
        model = TestFlow
        fields = ['id', 'document', 'workflow', 'workflow_code',
                  'matched_query', 'matched_query_name', 'status',
                  'current_stage', 'current_stage_name', 'current_sequence',
                  'company', 'context_snapshot', 'integration_status',
                  'lock_version', 'created_at', 'updated_at']


class TestDocumentSerializer(serializers.ModelSerializer):
    logs = TestDocumentLogSerializer(many=True, read_only=True)
    flow = serializers.SerializerMethodField()

    class Meta:
        model = TestDocument
        fields = ['id', 'company', 'branch', 'department', 'document_number',
                  'amount', 'status', 'logs', 'flow',
                  'created_at', 'updated_at']
        read_only_fields = ['status', 'created_at', 'updated_at']

    def get_flow(self, obj):
        flow = obj.flows.first()
        return TestFlowSerializer(flow).data if flow else None
