"""Serializers for workflow CONFIGURATION.

Configuration writes run the query validator on save (`WorkflowQuerySerializer`),
so a query cannot be stored as usable without passing it — `validated_at` is
never settable from the API.

There are no runtime serializers: tasks, actions and flow state belong to the
business module, which serialises its own.
"""
from rest_framework import serializers

from workflow.models import (
    COMPANY_ALL,
    COMPANY_VALUES,
    Workflow,
    WorkflowModule,
    WorkflowQuery,
    WorkflowStage,
    WorkflowUserReplacement,
)
from workflow.services import conditions, replacements


class WorkflowModuleSerializer(serializers.ModelSerializer):
    """Module identity, plus two read-only facts an operator needs.

    `workflow_count` is COMPUTED, never stored — it is what the Modules tab
    shows instead of the technical columns it used to: how much configuration
    hangs off this registration. Storing it would recreate the drift those
    columns were removed for.

    There is no `has_flow_model`. Flow models are module-owned now and the
    engine no longer resolves them, so it cannot honestly report on one.
    """

    #: Annotated by the list view; counted per row elsewhere.
    workflow_count = serializers.SerializerMethodField()

    class Meta:
        model = WorkflowModule
        fields = ['id', 'code', 'name', 'workflow_count',
                  'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']

    def get_workflow_count(self, obj):
        annotated = getattr(obj, 'workflow_count', None)
        return annotated if annotated is not None else obj.workflows.count()

    def validate_code(self, value):
        # The DB CHECK enforces this too; validating here turns a 500 into a
        # field error.
        if value != value.upper():
            raise serializers.ValidationError('Module code must be UPPERCASE.')
        return value

    def validate(self, attrs):
        """`code` is frozen once the module exists.

        The engine resolves a module BY CODE (`start()` is handed
        `WorkflowModule.objects.get(code=...)`), and modules call it with a
        literal. Renaming a live registration silently detaches every caller —
        the module stops starting workflows and nothing reports why. Register a
        new module instead; deactivate the old one.
        """
        if self.instance is not None and 'code' in attrs:
            if attrs['code'] != self.instance.code:
                raise serializers.ValidationError({
                    'code': (
                        f'The module code cannot be changed once registered — '
                        f'the engine and every caller resolve this module by '
                        f'"{self.instance.code}". Register a new module '
                        f'instead.'
                    )
                })
        return attrs


class _CompanyMixin:
    """Reject an unknown company as a field error, not a 500.

    The database CHECK (`*_company_valid`) is the guarantee; this turns the
    same rule into a readable 400 so a client never has to parse an
    IntegrityError. There is no longer a contradictory pair to reject — `ALL`
    is a value the one column holds, so "specific AND all" is unrepresentable
    rather than forbidden.
    """

    def _validate_company(self, attrs):
        company = (attrs['company'] if 'company' in attrs
                   else getattr(self.instance, 'company', None))
        if company is None:
            # Absent on create: the model default (`ALL`) applies.
            return attrs
        if company not in COMPANY_VALUES:
            raise serializers.ValidationError({
                'company': (
                    f'"{company}" is not a company. Use one of: '
                    f'{", ".join(COMPANY_VALUES)}.'
                )
            })
        return attrs


class WorkflowStageSerializer(serializers.ModelSerializer):
    """A stage, plus the context needed to read it outside its own workflow.

    The module/workflow/company fields are READ-ONLY projections of joins, not
    stored columns — the By User view lists stages from many workflows at once
    and has to say which. `effective_user` is resolved from replacements; it is
    reported ALONGSIDE the configured user rather than instead of it, because
    an administrator needs to see both to understand why somebody unexpected
    currently holds the work.

    Changing `user` here is the whole of "change this stage's user": the stage
    keeps its identity and everything already waiting at it simply resolves to
    the new person.
    """

    user_username = serializers.CharField(source='user.username',
                                          read_only=True)
    workflow_code = serializers.CharField(source='workflow.code',
                                          read_only=True)
    workflow_name = serializers.CharField(source='workflow.name',
                                          read_only=True)
    company = serializers.CharField(source='workflow.company', read_only=True)
    module_id = serializers.IntegerField(source='workflow.module_id',
                                         read_only=True)
    module_code = serializers.CharField(source='workflow.module.code',
                                        read_only=True)
    module_name = serializers.CharField(source='workflow.module.name',
                                        read_only=True)
    effective_user = serializers.SerializerMethodField()
    effective_user_username = serializers.SerializerMethodField()
    has_active_replacement = serializers.SerializerMethodField()

    class Meta:
        model = WorkflowStage
        fields = ['id', 'workflow', 'workflow_code', 'workflow_name',
                  'company', 'module_id', 'module_code', 'module_name',
                  'name', 'sequence', 'user', 'user_username',
                  'effective_user', 'effective_user_username',
                  'has_active_replacement',
                  'is_active', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']

    def _effective_id(self, obj):
        # The list view resolves every row in one query and passes the map in;
        # a single-object read falls back to looking this row up on its own.
        mapping = self.context.get('effective_map')
        if mapping is not None:
            return mapping.get(obj.user_id, obj.user_id)
        return replacements.effective_user_id(obj.user_id)

    def get_effective_user(self, obj):
        return self._effective_id(obj)

    def get_effective_user_username(self, obj):
        effective_id = self._effective_id(obj)
        if effective_id == obj.user_id:
            return obj.user.username
        mapping = self.context.get('effective_map') or {}
        name = mapping.get(f'name:{effective_id}')
        if name:
            return name
        from django.contrib.auth import get_user_model
        stand_in = get_user_model().objects.filter(pk=effective_id).first()
        return stand_in.username if stand_in else ''

    def get_has_active_replacement(self, obj):
        return self._effective_id(obj) != obj.user_id

    def validate_sequence(self, value):
        if value < 1:
            raise serializers.ValidationError('sequence starts at 1.')
        return value


class WorkflowQuerySerializer(_CompanyMixin, serializers.ModelSerializer):
    """Validation runs on save; `validated_at` is read-only by design.

    The whole configurable surface is `workflow`, `name`, `company` and
    `query_text`. `type` and `key_column` are gone — see the model for why —
    and are not accepted under any other name.
    """

    validation_problems = serializers.SerializerMethodField()

    class Meta:
        model = WorkflowQuery
        fields = ['id', 'workflow', 'name', 'company', 'query_text',
                  'validated_at', 'validation_error', 'validation_problems',
                  'is_active', 'created_at', 'updated_at']
        # A client must never be able to declare its own SQL validated.
        read_only_fields = ['validated_at', 'validation_error',
                            'created_at', 'updated_at']

    def get_validation_problems(self, obj):
        return obj.validation_error.split('\n') if obj.validation_error else []

    def validate(self, attrs):
        # Company first, so an invalid company is reported before the (much
        # more expensive) SQL round-trip — task §17's order.
        attrs = self._validate_company(attrs)

        # A query may only NARROW its workflow's company. Checked here because
        # a CHECK constraint cannot read the parent row.
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
        if workflow is not None and query_text:
            problems = conditions.check_query(workflow, query_text)
            if problems:
                raise serializers.ValidationError({'query_text': problems})
        company = (attrs['company'] if 'company' in attrs
                   else getattr(self.instance, 'company', COMPANY_ALL))
        if (workflow is not None
                and workflow.company != COMPANY_ALL
                and company != COMPANY_ALL
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


class WorkflowSerializer(_CompanyMixin, serializers.ModelSerializer):
    module_code = serializers.CharField(source='module.code', read_only=True)
    stages = WorkflowStageSerializer(many=True, read_only=True)
    queries = WorkflowQuerySerializer(many=True, read_only=True)

    class Meta:
        model = Workflow
        fields = ['id', 'module', 'module_code', 'code', 'name',
                  'company', 'is_active',
                  'stages', 'queries', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']

    def validate(self, attrs):
        return self._validate_company(attrs)


class WorkflowUserReplacementSerializer(serializers.ModelSerializer):
    old_username = serializers.CharField(source='old_user.username',
                                         read_only=True)
    new_username = serializers.CharField(source='new_user.username',
                                         read_only=True)

    class Meta:
        model = WorkflowUserReplacement
        fields = ['id', 'old_user', 'old_username', 'new_user', 'new_username',
                  'reason', 'start_date', 'end_date', 'is_active',
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


# ---------------------------------------------------------------------------
# No task / action / TestFlow serializers
# ---------------------------------------------------------------------------
#
# Those models are gone: a module owns its own approval runtime and history
# and serialises them itself. The engine's answer to a module is the plain
# dict from `selection.WorkflowSelection.as_dict()`, not a DRF serializer over
# an engine-owned table.
