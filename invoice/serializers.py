from rest_framework import serializers
from .models import InvoiceLog  , InvoiceRefLogs


class InvoiceLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceLog
        fields = '__all__'
        
class InvoiceRefLogsSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceRefLogs
        fields = '__all__'    
