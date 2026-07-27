from rest_framework import serializers
from .models import InvocieHistory, InvoiceLog , InvoiceRefLogs , CreditLimitLogs

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

    class Meta:
        model = InvoiceLog
        fields = '__all__'
        # Lineage is established by the create view after it has verified the
        # source is genuinely REJECTED — never taken from the request body.
        read_only_fields = ['supersedes']

    def get_supersedes_so_number(self, obj):
        return obj.supersedes.so_number if obj.supersedes_id else None

    def get_supersedes_status(self, obj):
        return obj.supersedes.status if obj.supersedes_id else None

    def get_supersedes_rejection_reason(self, obj):
        return obj.supersedes.rejection_reason if obj.supersedes_id else None

    def get_superseded_by_id(self, obj):
        replacement = obj.superseded_by.first()
        return replacement.pk if replacement else None

class InvoiceRefLogsSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceRefLogs
        fields = '__all__'

class CreditLimitLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = CreditLimitLogs
        fields = ['invoice_log' , 'jsap_doc_id' , 'party_name' , 'credit_raised']
    
