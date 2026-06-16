from rest_framework import serializers
from .models import InvocieHistory, InvoiceLog , InvocieHistory , InvoiceRefLogs

class InvoiveHistorySerializer(serializers.ModelSerializer):
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
