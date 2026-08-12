from rest_framework import serializers
from .models import InvocieHistory, InvoiceLog , InvoiceRefLogs , CreditLimitLogs
from .services.fg_stock import CONTEXT_KEY as FG_STOCK_CONTEXT_KEY, fg_stock_for_log

class InvoiveHistorySerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source='created_by.name', read_only=True)

    class Meta:
        model = InvocieHistory
        fields = '__all__'

class InvoiceLogSerializer(serializers.ModelSerializer):
    # Lineage, flattened so the review list can show "revision of rejected #42"
    # (and the reverse) without a round trip per row. Method fields rather than
    # dotted sources because `supersedes` is nullable on nearly every row.
    supersedes_so_number = serializers.SerializerMethodField()
    supersedes_status = serializers.SerializerMethodField()
    supersedes_rejection_reason = serializers.SerializerMethodField()
    superseded_by_id = serializers.SerializerMethodField()
    # Live HANA on-hand stock for every FG line in invoice_payload, so a reviewer
    # can see whether the warehouse can actually cover the invoice.
    fg_stock = serializers.SerializerMethodField()
    # Whether the Delete action should be offered for this row, so the review
    # screen does not have to keep its own copy of the deletable-status list.
    can_delete = serializers.SerializerMethodField()
    # default=None keeps the key present on live rows too — without it DRF drops
    # the field whenever deleted_by is null, so the shape of a row would change
    # depending on whether it had been deleted.
    deleted_by_name = serializers.CharField(source='deleted_by.name', read_only=True, default=None)

    class Meta:
        model = InvoiceLog
        fields = '__all__'
        # Lineage is established by the create view after it has verified the
        # source is genuinely REJECTED — never taken from the request body.
        # The delete stamps are set only by the delete/restore endpoints, which
        # enforce the status rules; a PATCH must not be able to bypass them.
        read_only_fields = [
            'supersedes',
            'is_deleted',
            'deleted_at',
            'deleted_by',
            'delete_reason',
        ]

    def get_supersedes_so_number(self, obj):
        return obj.supersedes.so_number if obj.supersedes_id else None

    def get_supersedes_status(self, obj):
        return obj.supersedes.status if obj.supersedes_id else None

    def get_supersedes_rejection_reason(self, obj):
        return obj.supersedes.rejection_reason if obj.supersedes_id else None

    def get_superseded_by_id(self, obj):
        replacement = obj.superseded_by.first()
        return replacement.pk if replacement else None

    def get_fg_stock(self, obj):
        return fg_stock_for_log(obj, self.context.get(FG_STOCK_CONTEXT_KEY))

    def get_can_delete(self, obj):
        return not obj.is_deleted and obj.status in InvoiceLog.DELETABLE_STATUSES

class InvoiceRefLogsSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceRefLogs
        fields = '__all__'

class CreditLimitLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = CreditLimitLogs
        fields = ['invoice_log' , 'jsap_doc_id' , 'party_name' , 'credit_raised']
    
