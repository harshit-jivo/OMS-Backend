"""Credit Limit API shapes. Stage and user names are read from the engine at
render time, never stored on this module's rows."""
from django.utils import timezone
from rest_framework import serializers

from core.companies import COMPANY_CODES

from credit_limit.models import CreditLimitActionLog, CreditLimitRequest


class CreditLimitFlowSerializer(serializers.Serializer):
    status = serializers.CharField()
    workflow_code = serializers.CharField(source='workflow.code')
    current_stage_name = serializers.CharField(
        source='current_stage.name', default='')
    current_stage_sequence = serializers.IntegerField(
        source='current_stage.sequence', default=None)
    current_user_username = serializers.CharField(
        source='current_user.username', default='')
    total_stage = serializers.IntegerField()
    sap_response = serializers.CharField()
    updated_at = serializers.DateTimeField()


class CreditLimitRequestSerializer(serializers.ModelSerializer):
    created_by_username = serializers.CharField(
        source='created_by.username', read_only=True, default='')
    attachment_name = serializers.SerializerMethodField()
    flow = CreditLimitFlowSerializer(read_only=True)

    class Meta:
        model = CreditLimitRequest
        fields = ['id', 'company', 'card_code', 'card_name', 'main_group',
                  'current_balance', 'current_credit_limit',
                  'new_credit_limit', 'valid_till', 'remarks',
                  'attachment_name', 'invoice_log', 'created_by',
                  'created_by_username', 'created_at', 'flow']
        read_only_fields = fields

    def get_attachment_name(self, obj):
        return obj.attachment.name.rsplit('/', 1)[-1] if obj.attachment else ''


class CreditLimitLineSerializer(serializers.Serializer):
    """One party in a submission. Its customer facts come from SAP."""

    card_code = serializers.CharField(max_length=50)
    new_credit_limit = serializers.DecimalField(
        max_digits=19, decimal_places=2, min_value=1)
    valid_till = serializers.DateField()

    def validate_valid_till(self, value):
        if value < timezone.localdate():
            raise serializers.ValidationError('Valid till cannot be in the past.')
        return value


class CreditLimitCreateSerializer(serializers.Serializer):
    """What the client may send: one company, one or more parties, and the
    remarks and supporting document they share.

    The document is REQUIRED for a single party and optional for several —
    `flow.attachment_required` is the rule; this only reports it per field.
    """

    company = serializers.ChoiceField(choices=COMPANY_CODES)
    lines = CreditLimitLineSerializer(many=True, allow_empty=False)
    remarks = serializers.CharField(required=False, allow_blank=True,
                                    default='')
    attachment = serializers.FileField(required=False, allow_null=True)

    def validate_lines(self, value):
        from credit_limit.services.flow import MAX_LINES

        if len(value) > MAX_LINES:
            raise serializers.ValidationError(
                f'At most {MAX_LINES} parties per submission.')
        codes = [line['card_code'].strip().upper() for line in value]
        if len(codes) != len(set(codes)):
            raise serializers.ValidationError(
                'Each party can appear once per submission.')
        return value

    def validate(self, attrs):
        from credit_limit.services.flow import attachment_required

        if attachment_required(len(attrs['lines'])) and not attrs.get('attachment'):
            raise serializers.ValidationError({'attachment': [
                'A supporting document is required for a single-party request.']})
        return attrs


class CreditLimitActionLogSerializer(serializers.ModelSerializer):
    acted_by_username = serializers.CharField(
        source='acted_by.username', read_only=True, default='')
    stage_name = serializers.CharField(source='stage.name', read_only=True,
                                       default='')

    class Meta:
        model = CreditLimitActionLog
        fields = ['id', 'action', 'stage', 'stage_name', 'acted_by',
                  'acted_by_username', 'remarks', 'acted_at']
        read_only_fields = fields


class DecisionSerializer(serializers.Serializer):
    remarks = serializers.CharField(required=False, allow_blank=True,
                                    default='')
