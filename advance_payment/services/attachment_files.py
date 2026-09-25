"""Read a SAP attachment's FILE from the attachment file service.

SAP keeps attachments on a Windows share per company; the FastAPI service on
.118 (`SAP_ATTACHMENT_FILES_URL`) reads them from there:

    GET {SAP_ATTACHMENT_FILES_URL}/{filename}?company=N

N is the SERVICE's company number (1 Oil, 2 Beverages, 3 Mart), mapped from
ours in settings (`SAP_ATTACHMENT_COMPANY_IDS`). It answers every file
ZIPPED, a single file in the archive, and 404 with `{"detail": ...}` when the
name is not on the share. This unwraps the zip, so the browser gets the PDF
or image itself and can show it inline.

The file NAME always comes from SAP (ATC1, via `sap.document_attachment`),
never from the caller, so this cannot be pointed at an arbitrary file.
"""

import io
import logging
import mimetypes
import zipfile
from urllib.parse import quote

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

#: A scanned invoice is a few MB; this is only a guard against a runaway zip.
MAX_FILE_BYTES = 50 * 1024 * 1024


class AttachmentNotConfigured(Exception):
    """The service URL, or this company's number for it, is not set."""


class AttachmentNotFound(Exception):
    """SAP names the file, but it is not on the attachments share."""


class AttachmentUnavailable(Exception):
    """The file service could not be reached, or answered something unusable."""


def _endpoint(company):
    base = getattr(settings, 'SAP_ATTACHMENT_FILES_URL', '')
    company_id = str(getattr(settings, 'SAP_ATTACHMENT_COMPANY_IDS', {}).get(company) or '').strip()
    if not base or not company_id:
        raise AttachmentNotConfigured(
            f'SAP attachments are not configured for {company}: set '
            f'SAP_ATTACHMENT_FILES_URL and SAP_ATTACHMENT_COMPANY_{company} in .env.')
    return base, company_id


def _unzip(payload, file_name):
    """The file inside the service's one-file zip."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise AttachmentUnavailable('The attachment service sent a damaged archive.') from exc
    members = [m for m in archive.infolist() if not m.is_dir()]
    if not members:
        raise AttachmentUnavailable('The attachment service sent an empty archive.')
    # The one named like the file, else the only one there is.
    member = next((m for m in members if m.filename.rsplit('/', 1)[-1] == file_name), members[0])
    if member.file_size > MAX_FILE_BYTES:
        raise AttachmentUnavailable('The attachment is too large to show.')
    return archive.read(member)


def fetch(company, file_name):
    """`(content, content_type)` of `file_name` on `company`'s share."""
    base, company_id = _endpoint(company)
    url = f'{base}/{quote(file_name, safe="")}'
    try:
        response = requests.get(
            url, params={'company': company_id},
            timeout=getattr(settings, 'SAP_ATTACHMENT_TIMEOUT', 30))
    except requests.RequestException as exc:
        logger.warning('advance_payment: attachment service unreachable (%s): %s', url, exc)
        raise AttachmentUnavailable('The SAP attachment service could not be reached.') from exc

    if response.status_code == 404:
        raise AttachmentNotFound(f'{file_name} is not on the SAP attachments share.')
    if response.status_code != 200:
        logger.warning('advance_payment: attachment service %s for %s: %s',
                       response.status_code, url, response.text[:300])
        raise AttachmentUnavailable(
            f'The SAP attachment service answered {response.status_code}.')

    content = response.content
    if content[:2] == b'PK':
        content = _unzip(content, file_name)
    content_type = mimetypes.guess_type(file_name)[0] or 'application/octet-stream'
    return content, content_type
