from .models import LabelData , LabelItem , NutritionUOM , LabelNutrition
from rest_framework import serializers


class LabelUploadSerializer(serializers.ModelSerializer):
    class Meta:
        model = LabelData
        fields = ['label_file']
        
class LabelItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = LabelItem
        fields = '__all__'
        
class NutritionUOMSerializer(serializers.ModelSerializer):
    class Meta:
        model =  NutritionUOM
        fields = '__all__'
        
class LabelNutritionSerializers(serializers.ModelSerializer):
    class Meta:
        model = LabelNutrition
        fields = '__all__'
        
        
        
        
        