from rest_framework import serializers

from .models import (
    ApprovalAction,
    ApprovalLevel,
    ApprovalLevelApprover,
    ApprovalRequest,
    ApprovalWorkflow,
)


class ApprovalLevelApproverSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source='user.name', read_only=True)
    username = serializers.CharField(source='user.username', read_only=True)

    class Meta:
        model = ApprovalLevelApprover
        fields = ['id', 'level', 'user', 'user_name', 'username',
                  'company', 'is_active', 'assigned_at']
        # `level` comes from the URL (/levels/<level_id>/approvers/) and is
        # applied in the view's perform_create. Leaving it writable made it a
        # REQUIRED body field, so validation rejected every create before
        # perform_create ever ran — the endpoint could not be used at all.
        read_only_fields = ['assigned_at', 'level']

    def validate(self, attrs):
        """Reject a duplicate grant with a 400 instead of a 500.

        (level, user, company) is UNIQUE in the database. Because `level` is
        read-only it never reaches validated_data, so DRF cannot build its
        usual UniqueTogetherValidator — without this check the constraint
        surfaces as an uncaught IntegrityError.
        """
        level_id = self.context.get('level_id')
        if level_id is None:
            return attrs

        user = attrs.get('user', getattr(self.instance, 'user', None))
        company = attrs.get('company', getattr(self.instance, 'company', ''))

        clash = ApprovalLevelApprover.objects.filter(
            level_id=level_id, user=user, company=company)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(
                {'user': 'This user is already an approver on this level '
                         'for that company.'})
        return attrs


class ApprovalLevelSerializer(serializers.ModelSerializer):
    role_name = serializers.CharField(source='role.name', read_only=True)
    approvers = ApprovalLevelApproverSerializer(many=True, read_only=True)
    # Auto-assigned on create (next in the ladder) so the admin never types a
    # number that could collide with the (workflow, sequence) unique constraint.
    # Reordering is done with the up/down arrows, which PATCH it explicitly.
    sequence = serializers.IntegerField(required=False, allow_null=True)
    # Never required in a payload — create() assigns it. Declared here as well
    # as on the field because ModelSerializer re-derives `required` from the
    # model, where the column is NOT NULL.
    min_approvals = serializers.IntegerField(required=False, default=1)

    class Meta:
        model = ApprovalLevel
        fields = ['id', 'workflow', 'sequence', 'name', 'role', 'role_name',
                  'min_approvals', 'is_active', 'approvers']
        # DRF derives a UniqueTogetherValidator from the (workflow, sequence)
        # constraint, and that validator FORCES every field it covers to be
        # required — overriding the `required=False` declared above and making
        # auto-assignment impossible. Cleared here so create() can fill the
        # sequence in; validate() below re-checks uniqueness, and the database
        # constraint still backs it up.
        validators = []

    def validate(self, attrs):
        """Guard (workflow, sequence) now that the auto-validator is off."""
        workflow = attrs.get('workflow', getattr(self.instance, 'workflow', None))
        sequence = attrs.get('sequence', getattr(self.instance, 'sequence', None))
        if workflow is None or sequence is None:
            return attrs          # create() assigns the next free sequence

        clash = ApprovalLevel.objects.filter(workflow=workflow, sequence=sequence)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError({
                'sequence': f'Level {sequence} already exists in this workflow.'
            })
        return attrs

    def create(self, validated_data):
        """Append to the end of the ladder unless a sequence was given."""
        if validated_data.get('sequence') is None:
            workflow = validated_data.get('workflow')
            last = (
                ApprovalLevel.objects.filter(workflow=workflow)
                .order_by('-sequence')
                .values_list('sequence', flat=True)
                .first()
            )
            validated_data['sequence'] = (last or 0) + 1
        # One approver per stage — the UI no longer offers this, so a stray
        # value in a payload must not create a stage nobody can clear.
        validated_data['min_approvals'] = 1
        return super().create(validated_data)


class ApprovalWorkflowSerializer(serializers.ModelSerializer):
    levels = ApprovalLevelSerializer(many=True, read_only=True)

    class Meta:
        model = ApprovalWorkflow
        fields = ['id', 'code', 'name', 'document_type', 'company',
                  'restart_on_reject', 'forbid_self_approval', 'is_active',
                  'levels', 'created_at']
        read_only_fields = ['created_at']
        # DRF derives a UniqueTogetherValidator from the model constraint and
        # runs it BEFORE validate(), reporting "must make a unique set" — which
        # says nothing about WHICH workflow blocks you or what to do. Silencing
        # it lets validate() below own the rule; the DB constraint still backs
        # it up, so nothing is weakened.
        validators = []

    def validate_code(self, value):
        """`code` is unique on the model. Clearing Meta.validators dropped the
        auto-generated check along with the unique-together one, so it is
        restored here rather than silently lost."""
        qs = ApprovalWorkflow.objects.filter(code=value)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                f'The code "{value}" is already used by another workflow.')
        return value

    def validate(self, attrs):
        """Only ONE active workflow may exist per (document type, company).

        The database enforces this with a partial unique constraint, but that
        surfaces as an IntegrityError/500. Checking here turns it into a 400
        that tells the admin exactly which workflow to deactivate first.

        A blank company means "all companies", and it collides only with
        another blank — a specific-company workflow legitimately overrides the
        catch-all (resolve_workflow prefers the specific one).
        """
        def current(field, default=None):
            if field in attrs:
                return attrs[field]
            return getattr(self.instance, field, default)

        # Nothing to guard: an inactive workflow is exempt from the constraint.
        if not current('is_active', True):
            return attrs

        document_type = current('document_type')
        company = current('company', '') or ''

        clash = ApprovalWorkflow.objects.filter(
            document_type=document_type, company=company, is_active=True)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)

        existing = clash.first()
        if existing:
            scope = f'company {company}' if company else 'all companies'
            raise serializers.ValidationError({
                'company': (
                    f'An active {document_type} workflow already covers {scope}: '
                    f'"{existing.name}" ({existing.code}). Deactivate it before '
                    f'creating a new one.'
                )
            })
        return attrs


class ApprovalActionSerializer(serializers.ModelSerializer):
    action_display = serializers.CharField(source='get_action_display', read_only=True)

    class Meta:
        model = ApprovalAction
        fields = ['id', 'sequence', 'round_number', 'level', 'level_name',
                  'action', 'action_display', 'remarks', 'approver',
                  'approver_username', 'approver_role', 'acted_at']


class ApprovalRequestSerializer(serializers.ModelSerializer):
    level_label = serializers.CharField(read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    workflow_code = serializers.CharField(source='workflow.code', read_only=True)
    document_type = serializers.CharField(
        source='workflow.document_type', read_only=True)
    submitted_by_name = serializers.CharField(
        source='submitted_by.name', read_only=True)

    class Meta:
        model = ApprovalRequest
        fields = ['id', 'workflow', 'workflow_code', 'document_type',
                  'company', 'amount', 'document_number',
                  'status', 'status_display', 'current_level', 'total_levels',
                  'level_label', 'round_number',
                  'submitted_by', 'submitted_by_name', 'submitted_at',
                  'level_entered_at', 'decided_at', 'created_at']


class ApprovalRequestDetailSerializer(ApprovalRequestSerializer):
    actions = ApprovalActionSerializer(many=True, read_only=True)

    class Meta(ApprovalRequestSerializer.Meta):
        fields = ApprovalRequestSerializer.Meta.fields + ['actions']


class ApprovalDecisionSerializer(serializers.Serializer):
    """Payload for approve / reject / cancel."""

    decision = serializers.ChoiceField(choices=['APPROVE', 'REJECT', 'CANCEL'])
    remarks = serializers.CharField(required=False, allow_blank=True, default='')

    def validate(self, attrs):
        # Mirrors the service-level rule so the client gets a field error rather
        # than a generic 400. The service re-checks it regardless — the API is
        # not the only write path.
        if attrs['decision'] == 'REJECT' and not attrs.get('remarks', '').strip():
            raise serializers.ValidationError(
                {'remarks': 'Remarks are mandatory when rejecting.'})
        return attrs
