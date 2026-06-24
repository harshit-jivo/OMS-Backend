from .models import LabelData
from rest_framework import serializers


class LabelUploadSerializer(serializers.ModelSerializer):
    class Meta:
        model = LabelData
        fields = ['label_file']