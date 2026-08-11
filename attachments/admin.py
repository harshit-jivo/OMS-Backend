from django.contrib import admin

from .models import Attachment


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    list_display = ('stored_name', 'attachment_type', 'original_name',
                    'uploaded_by', 'created_at')
    list_filter = ('attachment_type', 'created_at')
    search_fields = ('stored_name', 'original_name')
    readonly_fields = ('content_type', 'object_id', 'stored_name',
                       'original_name', 'uploaded_by', 'created_at', 'updated_at')

    def has_add_permission(self, request):
        # Files are only created through the upload API, which validates them.
        return False
