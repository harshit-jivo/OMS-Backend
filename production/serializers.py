"""PRDO API shapes.

Read-only over the business document: OMS does not create or edit a production
order, so there is no create/update serializer. The only thing a client sends
is a decision.
"""
from rest_framework import serializers

from production.models import (
    ProductionOrder,
    ProductionOrderActionLog,
    ProductionOrderFlow,
)


class FlowSerializer(serializers.ModelSerializer):
    workflow_code = serializers.CharField(source='workflow.code', read_only=True)
    current_stage_name = serializers.SerializerMethodField()
    current_stage_sequence = serializers.SerializerMethodField()
    current_user_name = serializers.SerializerMethodField()
    #: "Stage 2 of 3" — resolved from the stage, with the count snapshotted at
    #: submission so it stays correct after a stage is added or retired.
    stage_label = serializers.SerializerMethodField()

    class Meta:
        model = ProductionOrderFlow
        fields = [
            'id', 'status', 'sap_status', 'sap_status_text',
            'workflow', 'workflow_code', 'current_stage', 'current_stage_name',
            'current_stage_sequence', 'current_user', 'current_user_name',
            'total_stage', 'stage_label', 'created_at', 'updated_at',
        ]

    def get_current_stage_name(self, obj):
        return obj.current_stage.name if obj.current_stage_id else None

    def get_current_stage_sequence(self, obj):
        return obj.current_stage.sequence if obj.current_stage_id else None

    def get_current_user_name(self, obj):
        user = obj.current_user
        return getattr(user, 'username', None) if user else None

    def get_stage_label(self, obj):
        if not obj.current_stage_id:
            return None
        return f'Stage {obj.current_stage.sequence} of {obj.total_stage}'


class ProductionOrderSerializer(serializers.ModelSerializer):
    flow = FlowSerializer(read_only=True)
    #: Derived on read from the pack-size snapshots, never stored — a stored
    #: box count that disagreed with `planned_qty` would be a second, quieter
    #: source of truth.
    planned_boxes = serializers.SerializerMethodField()
    planned_litres = serializers.SerializerMethodField()
    #: True when SAP's release gate would not have applied to this order
    #: anyway. Surfaced rather than hidden — see PRDO_DESIGN.md §7.2.
    gate_exempt = serializers.SerializerMethodField()

    class Meta:
        model = ProductionOrder
        fields = [
            'id', 'company', 'sap_doc_entry', 'sap_doc_num',
            'item_code', 'item_name', 'item_group', 'item_series',
            'warehouse', 'planned_qty', 'planned_boxes', 'planned_litres',
            'sal_factor2', 'sal_pack_un', 'order_type',
            'post_date', 'due_date', 'start_date',
            'batch_no', 'mfg_date', 'expiry_date',
            'sap_created_by', 'sap_user_sign', 'gate_exempt',
            'remarks', 'sap_status', 'synced_at',
            'created_at', 'updated_at', 'flow',
        ]

    def _decimal(self, value):
        return None if value is None else str(value)

    def get_planned_boxes(self, obj):
        return self._decimal(obj.planned_boxes)

    def get_planned_litres(self, obj):
        return self._decimal(obj.planned_litres)

    def get_gate_exempt(self, obj):
        from production.services import gate
        return gate.is_exempt(obj)


class ActionLogSerializer(serializers.ModelSerializer):
    acted_by_name = serializers.SerializerMethodField()
    #: Resolved through the FK, not copied at write time, so a renamed stage
    #: reads correctly in history too.
    stage_name = serializers.SerializerMethodField()
    stage_sequence = serializers.SerializerMethodField()

    class Meta:
        model = ProductionOrderActionLog
        fields = [
            'id', 'action', 'acted_by', 'acted_by_name',
            'stage', 'stage_name', 'stage_sequence',
            'remarks', 'action_data', 'acted_at',
        ]

    def get_acted_by_name(self, obj):
        return getattr(obj.acted_by, 'username', None) if obj.acted_by_id else None

    def get_stage_name(self, obj):
        return obj.stage.name if obj.stage_id else None

    def get_stage_sequence(self, obj):
        return obj.stage.sequence if obj.stage_id else None


class DecisionSerializer(serializers.Serializer):
    """Approve. Remarks optional."""

    remarks = serializers.CharField(required=False, allow_blank=True,
                                    max_length=2000)


class RejectionSerializer(serializers.Serializer):
    """Reject. A reason is REQUIRED — a missing one is a 400, not a 409.

    JSAP told the requester nothing at all on rejection.
    """

    remarks = serializers.CharField(required=True, allow_blank=False,
                                    max_length=2000,
                                    error_messages={
                                        'blank': 'A reason is required when rejecting.',
                                        'required': 'A reason is required when rejecting.',
                                    })
