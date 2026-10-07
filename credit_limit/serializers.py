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


class CreditLimitAttachmentSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


class CreditLimitRequestSerializer(serializers.ModelSerializer):
    created_by_username = serializers.CharField(
        source='created_by.username', read_only=True, default='')
    attachments = CreditLimitAttachmentSerializer(many=True, read_only=True)
    flow = CreditLimitFlowSerializer(read_only=True)

    class Meta:
        model = CreditLimitRequest
        fields = ['id', 'company', 'card_code', 'card_name', 'main_group',
                  'current_balance', 'current_credit_limit',
                  'new_credit_limit', 'valid_till', 'remarks',
                  'attachments', 'invoice_log', 'created_by',
                  'created_by_username', 'created_at', 'flow']
        read_only_fields = fields


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

    At least one document is REQUIRED for a single party; for several they
    are optional —
    `flow.attachment_required` is the rule; this only reports it per field.
    """

    company = serializers.ChoiceField(choices=COMPANY_CODES)
    lines = CreditLimitLineSerializer(many=True, allow_empty=False)
    remarks = serializers.CharField(required=False, allow_blank=True,
                                    default='')
    attachments = serializers.ListField(
        child=serializers.FileField(), required=False, default=list)

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
        from credit_limit.services.flow import (
            MAX_ATTACHMENTS, attachment_required,
        )

        files = attrs.get('attachments') or []
        if attachment_required(len(attrs['lines'])) and not files:
            raise serializers.ValidationError({'attachments': [
                'A supporting document is required for a single-party request.']})
        if len(files) > MAX_ATTACHMENTS:
            raise serializers.ValidationError({'attachments': [
                f'At most {MAX_ATTACHMENTS} supporting documents per submission.']})
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
