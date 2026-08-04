"""Attachment download.

The existing project serves media only under DEBUG with no permission check at
all (OMS/urls.py:43-44), so anyone guessing a path gets the file. Cheque images
are customer bank instruments, so every read here goes through an explicit
authorisation check and the network path is never exposed to the client.
"""
import logging

from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from core.responses import fail

from .models import Attachment
from .storage import content_type_for, open_stored

logger = logging.getLogger(__name__)


def can_view_attachment(user, attachment):
    """Whether `user` may read this file.

    Delegates to the owning document when it exposes `can_be_viewed_by`, so the
    rule lives with the domain object rather than being duplicated here.
    """
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser or user.is_staff:
        return True

    document = attachment.document
    if document is None:
        return False                      # orphaned row — deny by default
    checker = getattr(document, 'can_be_viewed_by', None)
    if callable(checker):
        return bool(checker(user))
    # No rule declared on the document: fall back to the uploader only.
    return attachment.uploaded_by_id == user.id


class AttachmentDownloadView(APIView):
    """Stream a stored file after checking permission."""

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        attachment = get_object_or_404(
            Attachment.objects.select_related('uploaded_by', 'content_type'), pk=pk)

        if not can_view_attachment(request.user, attachment):
            logger.warning('Attachment %s download denied for user %s',
                           pk, request.user.id)
            return fail('You do not have permission to view this file.',
                        status=http_status.HTTP_403_FORBIDDEN)

        try:
            handle = open_stored(attachment.stored_name, attachment.attachment_type)
        except FileNotFoundError:
            return fail('The stored file could not be found on the share.',
                        status=http_status.HTTP_404_NOT_FOUND)
        except Exception:
            logger.exception('Attachment %s could not be read from the share', pk)
            return fail('The file store is currently unavailable.',
                        status=http_status.HTTP_502_BAD_GATEWAY)

        response = FileResponse(
            handle,
            content_type=content_type_for(attachment.stored_name),
            as_attachment=True,                       # never render inline
            filename=attachment.original_name,
        )
        # Belt and braces against content sniffing.
        response['X-Content-Type-Options'] = 'nosniff'
        return response
