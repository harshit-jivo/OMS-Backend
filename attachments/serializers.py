from rest_framework import serializers

from .models import Attachment


class AttachmentSerializer(serializers.ModelSerializer):
    """Read shape. Note there is NO path field — the network location is an
    internal detail and is never exposed to a client."""

    type_display = serializers.CharField(
        source='get_attachment_type_display', read_only=True)
    uploaded_by_name = serializers.CharField(
        source='uploaded_by.name', read_only=True)
    download_url = serializers.SerializerMethodField()

    class Meta:
        model = Attachment
        fields = ['id', 'attachment_type', 'type_display', 'stored_name',
                  'original_name', 'uploaded_by', 'uploaded_by_name',
                  'download_url', 'created_at']
        read_only_fields = fields

    def get_download_url(self, obj):
        return f'/api/payments/attachments/{obj.id}/download/'
