"""Write/read attachment files on the existing shared network storage.

This deliberately reuses the approach already proven by EINV_QR_SAVE_DIR
(einvoice/services.py:386-455): smbclient with an explicit session when
credentials are configured, plain filesystem I/O otherwise, and a
temp-write-then-rename so a reader never sees a half-written file.

Files land FLAT in the configured share — no company, year or month
sub-folders, and no Django MEDIA_ROOT involvement:

    \\JIVO-APP\\Payments\\Receive_Payments\\9f0c2dbe8d8f4f7c.jpg
    \\JIVO-APP\\Payments\\Deposit_Payments\\d8129abc9134be21.pdf
"""
import logging
import ntpath
import os
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError

from .models import AttachmentType

logger = logging.getLogger(__name__)

# Validation — kept deliberately strict. The existing upload endpoints
# (SKU/views.py:12, legal/views.py:12) validate nothing at all.
ALLOWED_EXTENSIONS = {'jpg', 'jpeg', 'png', 'pdf'}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024          # 5 MB

# Magic bytes, so a .jpg that is actually HTML is rejected. The extension alone
# is client-supplied and therefore not evidence of anything.
_MAGIC = {
    'jpg': (b'\xff\xd8\xff',),
    'jpeg': (b'\xff\xd8\xff',),
    'png': (b'\x89PNG\r\n\x1a\n',),
    'pdf': (b'%PDF-',),
}

_DEPOSIT_TYPES = {
    AttachmentType.DEPOSIT_SLIP,
    AttachmentType.DEPOSIT_RECEIPT,
}


def directory_for(attachment_type):
    """The configured share for this attachment type."""
    if attachment_type in _DEPOSIT_TYPES:
        directory = getattr(settings, 'DEPOSIT_PAYMENTS_IMAGES', '') or ''
        name = 'DEPOSIT_PAYMENTS_IMAGES'
    else:
        directory = getattr(settings, 'PAYMENTS_IMAGES', '') or ''
        name = 'PAYMENTS_IMAGES'
    if not directory:
        raise ValidationError(
            f'{name} is not configured; cannot store attachments.')
    return directory


def _extension(filename):
    ext = (filename or '').rsplit('.', 1)[-1].lower() if '.' in (filename or '') else ''
    return ext


def validate_upload(upload):
    """Reject anything outside the allow-list. Returns the lowercase extension.

    Raises ValidationError with a message safe to show the user.
    """
    ext = _extension(upload.name)
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError(
            f'Unsupported file type ".{ext or "?"}". '
            f'Allowed: {", ".join(sorted(ALLOWED_EXTENSIONS))}.')

    if upload.size > MAX_UPLOAD_BYTES:
        mb = upload.size / (1024 * 1024)
        raise ValidationError(f'File is {mb:.1f} MB; the maximum is 5 MB.')

    head = upload.read(8)
    upload.seek(0)                                  # rewind for the real write
    if not any(head.startswith(sig) for sig in _MAGIC[ext]):
        raise ValidationError(
            f'File content does not match its ".{ext}" extension.')
    return ext


def _uses_smb(directory):
    """UNC path plus configured credentials => go through smbclient."""
    return bool(getattr(settings, 'PAYMENTS_SMB_USERNAME', '')) and \
        str(directory).startswith('\\\\')


def _register_smb(directory):
    import smbclient

    server = str(directory).strip('\\/').replace('/', '\\').split('\\')[0]
    smbclient.register_session(
        server,
        username=settings.PAYMENTS_SMB_USERNAME,
        password=getattr(settings, 'PAYMENTS_SMB_PASSWORD', '') or '',
    )
    return smbclient


def save_upload(upload, attachment_type):
    """Write an uploaded file to the share under a fresh UUID name.

    Returns the stored filename. Validation must already have passed.
    """
    ext = _extension(upload.name)
    stored_name = f'{uuid.uuid4().hex[:20]}.{ext}'
    directory = directory_for(attachment_type)
    data = upload.read()

    if _uses_smb(directory):
        smbclient = _register_smb(directory)
        try:
            smbclient.makedirs(directory, exist_ok=True)
        except Exception:  # noqa: BLE001 — the share normally already exists
            pass
        path = ntpath.join(directory, stored_name)
        tmp = f'{path}.tmp'
        with smbclient.open_file(tmp, mode='wb') as fh:
            fh.write(data)
        try:
            smbclient.remove(path)                  # SMB rename won't overwrite
        except Exception:  # noqa: BLE001 — fine when it doesn't exist yet
            pass
        smbclient.rename(tmp, path)
    else:
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, stored_name)
        tmp = f'{path}.{os.getpid()}.tmp'
        with open(tmp, 'wb') as fh:
            fh.write(data)
        os.replace(tmp, path)                       # atomic swap into place

    logger.info('Stored payment attachment %s -> %s', upload.name, path)
    return stored_name


def open_stored(stored_name, attachment_type):
    """Open a stored file for reading. Caller is responsible for closing it."""
    directory = directory_for(attachment_type)
    # `stored_name` is a UUID we generated, but treat it as untrusted anyway —
    # basename() guarantees it can never escape the configured directory.
    safe_name = os.path.basename(str(stored_name))

    if _uses_smb(directory):
        smbclient = _register_smb(directory)
        return smbclient.open_file(ntpath.join(directory, safe_name), mode='rb')
    return open(os.path.join(directory, safe_name), 'rb')


def delete_stored(stored_name, attachment_type):
    """Best-effort removal from the share. Never raises."""
    try:
        directory = directory_for(attachment_type)
        safe_name = os.path.basename(str(stored_name))
        if _uses_smb(directory):
            smbclient = _register_smb(directory)
            smbclient.remove(ntpath.join(directory, safe_name))
        else:
            os.remove(os.path.join(directory, safe_name))
        return True
    except Exception:  # noqa: BLE001 — a missing file must not break the caller
        logger.warning('Could not delete attachment %s', stored_name, exc_info=True)
        return False


def content_type_for(stored_name):
    """MIME type for the download response."""
    ext = _extension(stored_name)
    return {
        'jpg': 'image/jpeg',
        'jpeg': 'image/jpeg',
        'png': 'image/png',
        'pdf': 'application/pdf',
    }.get(ext, 'application/octet-stream')
