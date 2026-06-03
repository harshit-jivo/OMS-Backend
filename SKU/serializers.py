from rest_framework import serializers
from .models import SKU

class SKUSerializer(serializers.ModelSerializer):
    class Meta:
        model = SKU
        fields = ['id', 'item_code', 'item_name', 'item_image', 'uploaded_at', 'updated_at']
