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
    # Just the extension, e.g. "pdf" or "jpeg" — enough for a client to pick
    # an icon without exposing the server-side filename or path.
    file_type = serializers.SerializerMethodField()

    class Meta:
        model = Attachment
        fields = ['id', 'attachment_type', 'type_display', 'file_type',
                  'uploaded_by', 'uploaded_by_name',
                  'download_url', 'created_at']
        read_only_fields = fields

    def get_file_type(self, obj):
        """The stored extension, lower-cased. '' when the path has none."""
        name = obj.stored_name
        return name.rsplit('.', 1)[-1].lower() if '.' in name else ''

    def get_download_url(self, obj):
        return f'/api/payments/attachments/{obj.id}/download/'
