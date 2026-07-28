from rest_framework import serializers
from .models import InvocieHistory, InvoiceLog , InvoiceRefLogs , CreditLimitLogs

class InvoiveHistorySerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source='created_by.name', read_only=True)

    class Meta:
        model = InvocieHistory
        fields = '__all__'

class InvoiceLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceLog
        fields = '__all__'

class InvoiceRefLogsSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceRefLogs
        fields = '__all__'

class CreditLimitLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = CreditLimitLogs
        fields = ['invoice_log' , 'jsap_doc_id' , 'party_name' , 'credit_raised']
    
