from rest_framework import serializers
from .models import InvocieHistory, InvoiceLog , InvocieHistory

class InvoiveHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = InvocieHistory
        fields = '__all__'

class InvoiceLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceLog
        fields = '__all__'
    
