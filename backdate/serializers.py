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
    normalise_company,
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
    #: Whether approving the CURRENT stage is the one that calls SAP.
    #:
    #: Decided here, by the same rule `flow.approve` uses, so the approval
    #: dialog can say "calling SAP" to the last approver and to nobody else.
    #: A client comparing `current_stage_sequence` with `total_stage` would be
    #: guessing: `total_stage` is the count at SUBMISSION, and a stage
    #: deactivated since then moves where the SAP call happens.
    is_final_stage = serializers.SerializerMethodField()

    def get_is_final_stage(self, obj):
        from backdate.services import flow as flow_service
        return flow_service.is_final_stage(obj)

    class Meta:
        model = BackDateFlow
        fields = ['id', 'backdate', 'status', 'hana_status', 'sap_payload',
                  'hana_status_text', 'workflow', 'workflow_code',
                  'current_user', 'current_user_username',
                  'effective_user_username', 'has_active_replacement',
                  'current_stage', 'current_stage_name',
                  'current_stage_sequence', 'total_stage',
                  'is_final_stage', 'created_at', 'updated_at']
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
    #: The companies as a list, so a client renders one badge each without
    #: splitting the string itself. `company` remains the canonical stored
    #: value (`"OIL,MART"`) and `company_label` its readable form
    #: (`"Oil, Mart"`) — three views of one fact, so no client has to invent
    #: a fourth.
    companies = serializers.ListField(
        child=serializers.CharField(), read_only=True)
    flow = BackDateFlowSerializer(read_only=True)
    #: Whether THIS caller may edit THIS request right now — the same rule the
    #: PATCH endpoint enforces, so the page can offer an Edit control only when
    #: it will actually work. Without a request in context (a serializer used
    #: outside a view) it is False: not offering an action is the safe default.
    can_edit = serializers.SerializerMethodField()

    def get_can_edit(self, obj):
        request = self.context.get('request')
        if request is None or not getattr(request, 'user', None):
            return False
        from backdate import permissions as bkdt_perms
        allowed, _reason, _why = bkdt_perms.may_edit(request.user, obj)
        return allowed

    class Meta:
        model = BackDate
        # No `remarks`: a request has no single remark. Each one belongs to
        # the event that produced it and is read from the history endpoint.
        # No `document_type`: the name IS the document identity now.
        fields = ['id', 'company', 'company_label', 'companies',
                  'sap_username', 'document_type_name',
                  'from_date', 'to_date', 'time_limit',
                  'action', 'action_label',
                  'created_by', 'created_by_username', 'created_at',
                  'updated_at', 'flow', 'can_edit']
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

    #: WRITE-ONLY, and not a model field: there is no remarks column. What the
    #: user types here becomes the `remarks` of the action-log row this
    #: submission writes — CREATE on a new request, UPDATE on an edit — so the
    #: reason is attached to the event it explains and to the person who gave
    #: it, rather than to a column that only ever holds the last one.
    remarks = serializers.CharField(
        required=False, allow_blank=True, max_length=2000,
        default='', write_only=True)

    class Meta:
        model = BackDate
        fields = ['company', 'sap_username', 'document_type_name',
                  'from_date', 'to_date', 'time_limit', 'action', 'remarks']

    def create(self, validated_data):
        # `remarks` is not a column. The view takes it from
        # `validated_data` before saving; dropping it here as well keeps a
        # direct `.save()` from raising on an unexpected kwarg.
        validated_data.pop('remarks', None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop('remarks', None)
        return super().update(instance, validated_data)

    def validate_document_type_name(self, value):
        """A SAP object NAME, checked against what SAP actually has.

        Checked HERE and not only at the SAP call because the number
        `OPEN_BKDT` needs is resolved from this name at approval time: a name
        SAP does not know would sail through every stage and fail at the very
        last one. Refusing it at submission costs one cached lookup.

        When SAP is unreachable the name is accepted as typed — a master-data
        outage must not stop somebody raising a request, and the SAP call
        remains the authority either way.
        """
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('The document type is required.')

        from backdate.services import sap_masters

        company = self.initial_data.get('company')
        if isinstance(company, list):
            company = company[0] if len(company) == 1 else None
        if not company and self.instance is not None:
            company = self.instance.company
        if not company:
            return value

        try:
            known = sap_masters.document_type_names(company)
        except sap_masters.MasterDataError:
            return value
        if not known:
            return value

        matched = [name for name in known
                   if name.strip().casefold() == value.casefold()]
        if not matched:
            raise serializers.ValidationError(
                f'SAP has no document type called "{value}" in {company}.')
        # Store SAP's own spelling, not the caller's.
        return matched[0]

    def validate_company(self, value):
        """One or more companies, normalised to their canonical spelling.

        A list is accepted — a form sends the ticked boxes — and so is an
        already-joined string, so a client may send `["OIL","MART"]` or
        `"OIL,MART"` and get the same request either way. Order and case are
        forgiven and duplicates collapse; an unknown code is refused.

        ONE POST, ONE REQUEST. A multi-company selection must never become
        several POSTs or several rows: the approval is one decision, and the
        companies only separate at the SAP call.
        """
        code, error = normalise_company(value)
        if error:
            raise serializers.ValidationError(error)
        return code

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
        fields = ['sap_username', 'document_type_name', 'from_date',
                  'to_date', 'time_limit', 'action', 'remarks']


#: The fields an edit may change, and therefore the only ones an UPDATE log
#: row can describe. Deliberately absent: `company` (see above), `created_by`
#: and the timestamps (not edits) — and `remarks`, which is not a field of the
#: request at all. An edit's reason is the log row's OWN `remarks`, so listing
#: it as a changed value would record the same sentence twice and invite the
#: two copies to differ.
TRACKED_FIELDS = ['company', 'sap_username', 'document_type_name',
                  'from_date', 'to_date', 'time_limit', 'action']


def snapshot(backdate):
    """The tracked fields, JSON-safe, for the change log.

    Every value is JSON-native: dates and timestamps as ISO strings, the
    document type as its SAP name. `payments.PaymentStatusHistory` keeps the
    same rule, and for the same reason — this column is read by a person, so
    the shape has to be stable.
    """
    def iso(value):
        return value.isoformat() if value is not None else None

    return {
        'company': backdate.company,
        'sap_username': backdate.sap_username,
        'document_type_name': backdate.document_type_name or '',
        'from_date': iso(backdate.from_date),
        'to_date': iso(backdate.to_date),
        'time_limit': iso(backdate.time_limit),
        'action': backdate.action,
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
