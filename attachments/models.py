"""Attachment metadata.

The FILE itself lives on the existing shared network storage
(\\JIVO-APP\\Payments\\...), written by storage.py using the same smbclient
approach as EINV_QR_SAVE_DIR. This table holds metadata only — there is no
FileField, because Django's storage layer is deliberately not involved.
"""
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models


class AttachmentType(models.TextChoices):
    """Which share a file belongs to, and what it depicts.

    The first two live in PAYMENTS_IMAGES, the last two in
    DEPOSIT_PAYMENTS_IMAGES — see storage.directory_for().
    """

    CHEQUE_IMAGE = 'CHEQUE_IMAGE', 'Cheque image'
    UPI_SCREENSHOT = 'UPI_SCREENSHOT', 'UPI screenshot'
    DEPOSIT_SLIP = 'DEPOSIT_SLIP', 'Bank deposit slip'
    DEPOSIT_RECEIPT = 'DEPOSIT_RECEIPT', 'Deposit receipt'


class Attachment(models.Model):
    """One uploaded file, attached generically to a payment or deposit."""

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    document = GenericForeignKey('content_type', 'object_id')

    attachment_type = models.CharField(
        max_length=20, choices=AttachmentType.choices, db_index=True)

    # THE identity of the file: the full path it was written to, captured at
    # write time. The basename is a generated UUID, e.g.
    # '9f0c2dbe8d8f4f7c.jpg' — the upload's own name is never used on disk,
    # being attacker-controlled and a route to collisions and traversal.
    #
    # `stored_name` and `original_name` used to live here too and were removed:
    # the first was exactly this column's basename (verified across every row
    # before removal), and the second was the upload's own filename, which a
    # camera supplies as a UUID — it labelled nothing and was shown to nobody
    # once the UI switched to naming attachments by type.
    #
    # UNIQUE moved here from `stored_name` and is the stronger guarantee: two
    # files may not occupy the same path, whereas two different directories
    # could legitimately hold the same basename.
    stored_path = models.CharField(max_length=500, unique=True)

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='payment_attachments')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'payment_attachment'
        ordering = ['-created_at']
        indexes = [
            # GenericForeignKey has no FK and therefore no automatic index —
            # without this, "attachments for this receipt" is a full scan.
            models.Index(fields=['content_type', 'object_id'], name='idx_att_target'),
        ]

    @property
    def stored_name(self):
        """The file's basename, derived rather than stored.

        Kept as a property because the storage layer still needs a filename —
        for the MIME type and for the safety check that a path may not point
        at a file other than the one it names. Splitting on both separators:
        a Windows share path read on a POSIX server would otherwise come back
        whole.
        """
        return str(self.stored_path).replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]

    def __str__(self):
        return f'{self.stored_name} ({self.get_attachment_type_display()})'
