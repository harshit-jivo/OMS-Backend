"""BackDate API shapes.

READ SHAPES DERIVE, THEY DO NOT DUPLICATE. A stage's name and sequence, and an
approver's name, are read from the engine at render time rather than stored on
BKDT rows — so a renamed stage or a reassigned user reads correctly everywhere
without this module updating anything.
"""
from django.utils import timezone
from rest_framework import serializers

from core.companies import COMPANY_CODES

from backdate.models import (
    ACTION_VALUES,
    BackDate,
    BackDateActionLog,
    BackDateFlow,
    normalise_action,
    normalise_companies,
)


def _stage_names(stage_ids):
    """`{id: name}` for a set of engine stages, in one query."""
    from workflow.models import WorkflowStage

    ids = {i for i in stage_ids if i}
    if not ids:
        return {}
    return dict(WorkflowStage.objects.filter(pk__in=ids)
                .values_list('pk', 'name'))


class BackDateActionLogSerializer(serializers.ModelSerializer):
    """One history row.

    `stage_name` is RESOLVED, not stored — see the module docstring. It is
    supplied by the view through `context['stage_names']` so a list of fifty
    log rows costs one extra query rather than fifty.
    """

    acted_by_username = serializers.CharField(
        source='acted_by.username', read_only=True, default='')
    action_label = serializers.CharField(
        source='get_action_display', read_only=True)
    stage_name = serializers.SerializerMethodField()

    class Meta:
        model = BackDateActionLog
        fields = ['id', 'backdate', 'action', 'action_label', 'stage',
                  'stage_name', 'acted_by', 'acted_by_username', 'remarks',
                  'action_data', 'acted_at']
        read_only_fields = fields

    def get_stage_name(self, obj):
        if not obj.stage_id:
            return ''
        names = self.context.get('stage_names')
        if names is not None:
            return names.get(obj.stage_id, '')
        return _stage_names([obj.stage_id]).get(obj.stage_id, '')


class BackDateFlowSerializer(serializers.ModelSerializer):
    """Where the request is now.

    `current_stage_name`, `current_stage_sequence` and `current_user_username`
    all come from the engine. Nothing here is a stored copy.

    `sap_payload` and `hana_status_text` ARE stored, and deliberately: they
    record what was actually sent to SAP and what SAP said back, per company.
    Neither is reproducible from the request afterwards, and when one company
    succeeds while another fails they are the only place that says which.
    """

    workflow_code = serializers.CharField(
        source='workflow.code', read_only=True, default='')
    current_stage_name = serializers.CharField(
        source='current_stage.name', read_only=True, default='')
    current_stage_sequence = serializers.IntegerField(
        source='current_stage.sequence', read_only=True, default=None)
    current_user_username = serializers.CharField(
        source='current_user.username', read_only=True, default='')
    #: Who may act TODAY, replacements applied. Usually the same as
    #: `current_user`; different the moment a delegation window opens or an
    #: administrator reassigns the stage, which is exactly when a requester
    #: chasing an approval needs to be told the truth.
    effective_user_username = serializers.SerializerMethodField()
    has_active_replacement = serializers.SerializerMethodField()

    class Meta:
        model = BackDateFlow
        fields = ['id', 'backdate', 'status', 'hana_status', 'sap_payload',
                  'hana_status_text', 'workflow', 'workflow_code',
                  'current_user', 'current_user_username',
                  'effective_user_username', 'has_active_replacement',
                  'current_stage', 'current_stage_name',
                  'current_stage_sequence', 'total_stage',
                  'created_at', 'updated_at']
        read_only_fields = fields

    def _assignment(self, obj):
        if not obj.current_stage_id:
            return None
        from workflow.services.assignments import get_stage_assignment
        return get_stage_assignment(obj.current_stage_id)

    def get_effective_user_username(self, obj):
        assignment = self._assignment(obj)
        return assignment.effective_username if assignment else ''

    def get_has_active_replacement(self, obj):
        assignment = self._assignment(obj)
        return bool(assignment and assignment.has_active_replacement)


class BackDateSerializer(serializers.ModelSerializer):
    """Read shape for one request."""

    created_by_username = serializers.CharField(
        source='created_by.username', read_only=True, default='')
    #: Neither of these is `get_*_display`: `action` and `company` are no
    #: longer `choices` fields, because a combination is not one of the atoms.
    #: See `BackDate`.
    action_label = serializers.CharField(read_only=True)
    company_label = serializers.CharField(read_only=True)
    #: The selection as a list, so a client renders it without splitting a
    #: string and guessing the separator.
    companies = serializers.ListField(
        child=serializers.CharField(), read_only=True)
    flow = BackDateFlowSerializer(read_only=True)

    class Meta:
        model = BackDate
        fields = ['id', 'company', 'company_label', 'companies',
                  'sap_username',
                  'document_type', 'from_date', 'to_date', 'time_limit',
                  'action', 'action_label', 'remarks',
                  'created_by', 'created_by_username', 'created_at',
                  'updated_at', 'flow']
        read_only_fields = fields


class _BackDateWriteSerializer(serializers.ModelSerializer):
    """Validation shared by create and edit.

    `created_by` is deliberately absent: it comes from the authenticated user
    in the view and is not accepted from the client under any name. JSAP took
    it from the request body, so a request could be raised in someone else's
    name.
    """

    #: A list or a comma-separated string — `validate_company` canonicalises
    #: either. Declared loosely on purpose: the model column is one string, but
    #: a form naturally sends the ticked boxes as a list.
    company = serializers.JSONField()

    class Meta:
        model = BackDate
        fields = ['company', 'sap_username', 'document_type', 'from_date',
                  'to_date', 'time_limit', 'action', 'remarks']

    def validate_company(self, value):
        """One or more companies, stored together on ONE request.

        Accepts a list or a comma-separated string, and returns the canonical
        spelling — so `["BEVERAGES","OIL"]` and `"OIL,BEVERAGES"` are the same
        request rather than two that look different in every report.
        """
        canonical, unknown = normalise_companies(value)
        if unknown:
            raise serializers.ValidationError(
                f'{", ".join(unknown)} is not a company. Use one or more of: '
                f'{", ".join(COMPANY_CODES)}.')
        if not canonical:
            raise serializers.ValidationError('At least one company is required.')
        return canonical

    def validate_sap_username(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('The SAP user is required.')
        # HANA's OPEN_BKDT declares USERID as NVARCHAR(20); a longer value
        # would be truncated on the way in, granting rights to a name nobody
        # asked for.
        if len(value) > 20:
            raise serializers.ValidationError(
                'SAP usernames are at most 20 characters.')
        return value

    def validate_action(self, value):
        """`'A'`, `'U'`, or both — and both is ONE request, not two.

        SAP is never told the action, so asking for Add and Update together is
        a single grant with a wider recorded scope. Accepting the pair here is
        what keeps the SAP row count following companies rather than actions.
        """
        normalised = normalise_action(value)
        if normalised not in ACTION_VALUES:
            raise serializers.ValidationError(
                'Action must be A, U, or "A,U" for both.')
        return normalised

    def validate_time_limit(self, value):
        """An expiry is mandatory — SAP ignores a grant without one.

        `SBO_SP_TRANSACTIONNOTIFICATION` ends all 14 of its BKDT lookups with
        `CURRENT_TIMESTAMP < r."timeLimit"`. Against a NULL that is UNKNOWN,
        so the grant never matches and the user still cannot post, having been
        told they were approved. The model refuses the NULL as well; this is
        the layer that says WHY.
        """
        if value is None:
            raise serializers.ValidationError(
                'An expiry is required — SAP ignores rights that never lapse.')
        return value

    def validate(self, attrs):
        # On a PATCH the unsent fields keep their stored values, so the rules
        # below are checked against the RESULT of the edit, not the payload.
        def resolved(name):
            if name in attrs:
                return attrs[name]
            return getattr(self.instance, name, None)

        from_date = resolved('from_date')
        to_date = resolved('to_date')
        time_limit = resolved('time_limit')

        if from_date and to_date and to_date < from_date:
            raise serializers.ValidationError(
                {'to_date': 'The end of the window cannot be before its start.'})

        # JSAP enforced this in SQL only. It matters: a time limit before the
        # window starts grants rights that have already lapsed.
        if time_limit and from_date and time_limit.date() < from_date:
            raise serializers.ValidationError(
                {'time_limit': 'The expiry cannot be before the window starts.'})

        if time_limit and time_limit < timezone.now():
            raise serializers.ValidationError(
                {'time_limit': 'The expiry is already in the past.'})

        return attrs


class BackDateCreateSerializer(_BackDateWriteSerializer):
    """Write shape for a new request."""


class BackDateUpdateSerializer(_BackDateWriteSerializer):
    """Write shape for editing a request that is still pending.

    `company` is absent: changing it would change which workflow applies, and
    the request has already been routed. Raising a new request is the honest
    way to ask for a different set of companies.
    """

    company = None

    class Meta(_BackDateWriteSerializer.Meta):
        fields = ['sap_username', 'document_type', 'from_date', 'to_date',
                  'time_limit', 'action', 'remarks']


#: The fields an edit may change, and therefore the only ones an UPDATE log
#: row can describe. Deliberately absent: `company` (see above), `created_by`
#: and the timestamps (not edits).
TRACKED_FIELDS = ['company', 'sap_username', 'document_type', 'from_date',
                  'to_date', 'time_limit', 'action', 'remarks']


def snapshot(backdate):
    """The tracked fields, JSON-safe, for the change log.

    Every value is JSON-native: dates and timestamps as ISO strings, the
    document type as a real number. `payments.PaymentStatusHistory` keeps the
    same rule, and for the same reason — this column is read by a person, so
    the shape has to be stable.
    """
    def iso(value):
        return value.isoformat() if value is not None else None

    return {
        'company': backdate.company,
        'sap_username': backdate.sap_username,
        'document_type': backdate.document_type,
        'from_date': iso(backdate.from_date),
        'to_date': iso(backdate.to_date),
        'time_limit': iso(backdate.time_limit),
        'action': backdate.action,
        'remarks': backdate.remarks or '',
    }


def diff(before, after):
    """`{field: {'old': ..., 'new': ...}}` for the fields that CHANGED.

    None when nothing did: an UPDATE row with an empty object would claim an
    edit it cannot describe, and `{}` reads as "we did not look".
    """
    if not before:
        return None
    changed = {
        key: {'old': before.get(key), 'new': after.get(key)}
        for key in after
        if before.get(key) != after.get(key)
    }
    return changed or None


class ApprovalDecisionSerializer(serializers.Serializer):
    """Approve/reject payload. Note what is NOT here: a user id.

    The acting user is the authenticated one. JSAP read `UserId` from this
    payload, which is why its approval endpoint could be driven as anybody.
    """

    remarks = serializers.CharField(
        required=False, allow_blank=True, max_length=2000, default='')
