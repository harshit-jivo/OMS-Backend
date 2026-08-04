"""Attachment create/delete, wrapping the share I/O in a transaction."""
import logging

from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from .models import Attachment
from .storage import delete_stored, save_upload, validate_upload

logger = logging.getLogger(__name__)


def attach(*, document, upload, attachment_type, user):
    """Validate, write to the share, and record the metadata row.

    The file is written BEFORE the row is created, and the row creation is
    atomic. If the DB write then fails, the orphaned file is removed so the
    share does not accumulate unreferenced blobs.
    """
    validate_upload(upload)
    original_name = (upload.name or 'file')[:255]
    stored_name = save_upload(upload, attachment_type)

    try:
        with transaction.atomic():
            return Attachment.objects.create(
                content_type=ContentType.objects.get_for_model(document.__class__),
                object_id=document.pk,
                attachment_type=attachment_type,
                stored_name=stored_name,
                original_name=original_name,
                uploaded_by=user,
            )
    except Exception:
        delete_stored(stored_name, attachment_type)
        raise


def detach(attachment):
    """Remove the row, then the file. Row first so a stale file is the worst
    case rather than a row pointing at nothing."""
    stored_name = attachment.stored_name
    attachment_type = attachment.attachment_type
    attachment.delete()
    delete_stored(stored_name, attachment_type)


def for_document(document):
    return Attachment.objects.filter(
        content_type=ContentType.objects.get_for_model(document.__class__),
        object_id=document.pk,
    ).select_related('uploaded_by')
