"""Read serializer for the reusable notification framework.

Exposes a recipient's own notifications to the app. Business-module-agnostic:
the entity is surfaced GENERICALLY as ``entity_type`` / ``entity_id`` (from the
canonical payload builder), never as an order/payment/deposit FK. A coarse
``module`` label is derived from the entity model name purely for client-side
grouping/filtering — the framework still stores no business concept.
"""

from rest_framework import serializers

from .models import Notification

# entity model name (content_type.model) -> coarse module label the client
# filters on. Additive: a future module adds one row here, nothing else.
_ENTITY_MODULE = {
    "paymentreceipt": "payments",
    "bankdeposit": "deposits",
}


class NotificationSerializer(serializers.ModelSerializer):
    entity_type = serializers.SerializerMethodField()
    entity_id = serializers.IntegerField(source="object_id", read_only=True)
    module = serializers.SerializerMethodField()

    class Meta:
        model = Notification
        fields = [
            "id",
            "event_type",
            "title",
            "message",
            "entity_type",
            "entity_id",
            "module",
            "company_id",
            "is_read",
            "created_at",
        ]
        read_only_fields = fields

    def get_entity_type(self, obj):
        ct = obj.content_type
        return ct.model if ct else None

    def get_module(self, obj):
        ct = obj.content_type
        model = ct.model if ct else ""
        return _ENTITY_MODULE.get(model, "other")
