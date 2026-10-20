"""A payment request's files, attached to its SAP outgoing payment.

WHERE EACH FILE GOES
--------------------
SAP keeps a company's attachments on its own share on .52, and Finance
attaches the PO, the contract mail or the bill to nearly every outgoing
payment they post by hand (Oil: 95% of supplier payments). OMS does the same
for the payments it posts, in two steps:

1. ON THE SHARE, as soon as a file is added (raising the request, an edit,
   Payment's bank proof, a payment proof). The file upload service on .118:8013
   (`docs/file-upload-utility.md`) writes it to the REQUEST'S COMPANY'S folder,
   by the service's folder id (`FILE_UPLOAD_FOLDERS`: 3 Oil, 4 Beverages,
   5 Mart). The service renames on the way in, so its id and stored name are
   kept on the `RequestFile`. A request whose company changes has its files
   put on the new company's share. A failure is recorded on the file
   (`share_error`) and never stops the request: posting tries again.

2. IN SAP, when the payment posts. An `Attachments2` row is made from the
   files already on the share (SourcePath: the folder as the Service Layer
   host mounts it, `SAP_ATTACHMENT_SOURCE_PATHS`), and its number goes on the
   outgoing payment (`AttachmentEntry`). If that fails, the payment posts
   without its files rather than hold the money up; the reason is kept on the
   voucher and `attach_after_posting` attaches them later (also how files
   added after posting, such as payment proofs, reach SAP).

   When the Service Layer refuses because it cannot see the share (its mount
   on .222 dropped: -5002 "Source file does not exist" / "Attachments folder
   ... changed or removed"), the attachment is written straight into SAP's
   tables instead, as SAP's own client would write it for a file already in
   the attachments folder (`sap_attachment_rows`, `SAP_ATTACHMENT_HANA_FALLBACK`).

The Service Layer cannot delete an attachment row (error 220), so a row is
made once per set of files and reused: a retried post whose files are already
in an attachment reuses that attachment rather than leave another orphan.
"""
import logging

import requests
from django.conf import settings
from django.db import transaction

from advance_payment.models import FilePurpose, LogAction, RequestFile, VoucherObject, VoucherStatus
from advance_payment.services import sap_attachment_rows

logger = logging.getLogger(__name__)

#: SAP attaches what Finance would see beside the payment: the request's own
#: files and the bank details' proof. (Payment proofs are added after posting,
#: and go through `attach_after_posting`.)
PURPOSES = (FilePurpose.SUPPORTING, FilePurpose.BANK_PROOF, FilePurpose.PAYMENT_PROOF)


class ShareError(Exception):
    """The file service did not store the file."""


# ---------------------------------------------------------------------------
# The file upload service
# ---------------------------------------------------------------------------

def enabled():
    return bool(getattr(settings, 'FILE_UPLOAD_URL', '') and getattr(settings, 'FILE_UPLOAD_TOKEN', ''))


def folder_for(company):
    return (getattr(settings, 'FILE_UPLOAD_FOLDERS', {}) or {}).get((company or '').upper())


def _headers(uploader=''):
    headers = {'X-API-Key': settings.FILE_UPLOAD_TOKEN}
    if uploader:
        headers['X-Uploader'] = uploader
    return headers


def _upload(folder, name, content, content_type, uploader):
    """`{id, stored_name}` of the one file stored. HTTP 200 may still be a refusal: read `failures`."""
    try:
        response = requests.post(
            f'{settings.FILE_UPLOAD_URL}/upload', headers=_headers(uploader),
            data={'folder_id': folder}, files={'files': (name, content, content_type)},
            timeout=getattr(settings, 'FILE_UPLOAD_TIMEOUT', 60))
    except requests.RequestException as exc:
        raise ShareError(f'The file service could not be reached: {exc}') from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise ShareError(f'The file service answered {response.status_code}.') from exc
    if body.get('status') == 'error' or response.status_code >= 400:
        error = body.get('error') or {}
        raise ShareError(error.get('message') or f'The file service answered {response.status_code}.')
    data = body.get('data') or {}
    if data.get('failures'):
        raise ShareError(data['failures'][0].get('message') or 'The file service refused the file.')
    stored = (data.get('files') or [None])[0]
    if not stored:
        raise ShareError('The file service stored nothing.')
    return stored


def _delete(file_id):
    """Remove a copy from the share; best effort (a stray copy harms nothing)."""
    try:
        requests.delete(f'{settings.FILE_UPLOAD_URL}/files/{int(file_id)}', headers=_headers(),
                        timeout=getattr(settings, 'FILE_UPLOAD_TIMEOUT', 60))
    except requests.RequestException as exc:
        logger.warning('advance_payment: could not remove shared file %s: %s', file_id, exc)


# ---------------------------------------------------------------------------
# Step 1: on the company's share
# ---------------------------------------------------------------------------

def _uploader(row):
    user = row.uploaded_by
    return (getattr(user, 'email', '') or getattr(user, 'username', '')) if user else ''


def share_file(row, company):
    """Put one file on `company`'s share, unless it is there already. True when it is."""
    folder = folder_for(company)
    if folder is None:
        row.share_error = f'No file service folder is set for {company}.'
        row.save(update_fields=['share_error'])
        return False
    if row.share_file_id and row.share_folder == folder:
        return True
    old = row.share_file_id
    try:
        with row.file.open('rb') as handle:
            stored = _upload(folder, row.name, handle.read(), 'application/octet-stream', _uploader(row))
    except (ShareError, OSError) as exc:
        row.share_error = str(exc)[:300]
        row.save(update_fields=['share_error'])
        logger.warning('advance_payment: %s not put on the %s share: %s', row.name, company, exc)
        return False
    row.share_file_id = stored.get('id')
    row.share_name = (stored.get('stored_name') or row.name)[:255]
    row.share_folder = folder
    row.share_error = ''
    # On another company's share: no longer the attachment it was a line of.
    row.sap_attachment_entry = None if old else row.sap_attachment_entry
    row.save(update_fields=['share_file_id', 'share_name', 'share_folder', 'share_error',
                            'sap_attachment_entry'])
    if old:
        _delete(old)  # the copy on the company it no longer belongs to
    return True


def share_request_files(advance):
    """Every file of the request on its company's share. `(shared, failed)` counts."""
    if not enabled():
        return 0, 0
    shared = failed = 0
    for row in RequestFile.objects.filter(request=advance, purpose__in=PURPOSES).select_related('uploaded_by'):
        if share_file(row, advance.company):
            shared += 1
        else:
            failed += 1
    return shared, failed


def share_after_commit(advance):
    """`share_request_files` once the save that added the files has committed.

    After, not inside: a request that rolls back must not leave its files on
    SAP's share, and the database transaction must not wait on the network.
    """
    if not enabled():
        return
    pk, company = advance.pk, advance.company

    def run():
        from advance_payment.models import AdvanceRequest

        fresh = AdvanceRequest.objects.filter(pk=pk).first()
        if fresh is not None:
            try:
                share_request_files(fresh)
            except Exception:  # noqa: BLE001 — never fail the request over its copy
                logger.exception('advance_payment: sharing the files of %s (%s) failed', pk, company)

    transaction.on_commit(run)


def unshare_after_commit(file_ids):
    """Remove deleted files' copies from the share, once the delete has committed."""
    ids = [i for i in file_ids if i]
    if ids and enabled():
        transaction.on_commit(lambda: [_delete(i) for i in ids])


# ---------------------------------------------------------------------------
# Step 2: in SAP
# ---------------------------------------------------------------------------

def _source_path(company):
    return (getattr(settings, 'SAP_ATTACHMENT_SOURCE_PATHS', {}) or {}).get((company or '').upper(), '')


def _line(row, source):
    stem, _, ext = row.share_name.rpartition('.')
    if not stem:  # a name with no extension
        stem, ext = row.share_name, ''
    return {'SourcePath': source, 'FileName': stem, 'FileExtension': ext, 'Override': 'tYES'}


#: What the Service Layer says when it cannot see the attachments share.
FOLDER_REFUSALS = ('source file does not exist', 'attachments folder', 'linux mount point')


def _folder_refused(exc):
    """The Service Layer refused only because its mount of the share is down."""
    text = f'{_sap_problem(exc)}'.lower()
    return str(getattr(exc, 'sap_code', '')) in ('-5002', '-43') and any(t in text for t in FOLDER_REFUSALS)


def _files(rows):
    return [(r.share_name, getattr(r, 'size', None)) for r in rows]


def _sap_problem(exc):
    payload = getattr(exc, 'payload', None) or {}
    message = ((payload.get('error') or {}).get('message') if isinstance(payload, dict) else '') or str(exc)
    if isinstance(message, dict):
        message = message.get('value', '')
    if str(getattr(exc, 'sap_code', '')) == '-5002':
        # The file is on the share (the file service put it there); the SAP
        # server cannot see it through its mount of the share.
        message += (' (the file is on the share, but the SAP server .222 cannot see the share through its'
                    ' mount: have the mount fixed on .222, then press Attach to SAP)')
    return f'SAP did not take the attachment: {message}'.strip()


def _new_attachment(rows, company, company_db):
    """Make one SAP attachment of `rows`; mark them with it. `(entry, problem)`."""
    from payments import sap_client

    source = _source_path(company)
    if not source:
        return None, f'No SAP attachment source folder is set for {company}.'
    try:
        _, body = sap_client.request('POST', '/Attachments2', company_db=company_db,
                                     json_body={'Attachments2_Lines': [_line(r, source) for r in rows]})
    except sap_client.SapError as exc:
        if not (_folder_refused(exc) and sap_attachment_rows.enabled()):
            return None, _sap_problem(exc)
        try:
            body = {'AbsoluteEntry': sap_attachment_rows.create(company_db, _files(rows))}
        except sap_attachment_rows.RowsError as rows_exc:
            return None, f'{_sap_problem(exc)}; written directly neither: {rows_exc}'
    entry = body.get('AbsoluteEntry') if isinstance(body, dict) else None
    if not entry:
        return None, 'SAP made the attachment but did not say its number.'
    RequestFile.objects.filter(pk__in=[r.pk for r in rows]).update(sap_attachment_entry=entry)
    return int(entry), ''


def _ready(advance):
    """The request's files on the share, and a summary of those that are not."""
    share_request_files(advance)
    rows = list(RequestFile.objects.filter(request=advance, purpose__in=PURPOSES).order_by('uploaded_on', 'pk'))
    ready = [r for r in rows if r.share_file_id and r.share_folder == folder_for(advance.company)]
    missing = [f'{r.name} ({r.share_error or "not on the share"})' for r in rows if r not in ready]
    return ready, missing


def for_payment(advance, company_db):
    """The attachment to post the payment with. `(entry or None, problem)`.

    None with no problem: the request has no files. A problem never stops the
    payment; the caller posts without the attachment and keeps the reason.
    """
    if not enabled():
        return None, ''
    ready, missing = _ready(advance)
    if not ready:
        return None, ('Files not attached: ' + '; '.join(missing)) if missing else ''
    entries = {r.sap_attachment_entry for r in ready}
    if len(entries) == 1 and None not in entries:
        entry = entries.pop()  # an earlier attempt's: reuse it, SAP cannot delete it
    else:
        entry, problem = _new_attachment(ready, advance.company, company_db)
        if problem:
            return None, problem
    note = ('Not attached: ' + '; '.join(missing)) if missing else ''
    return entry, note


def attach_after_posting(advance, *, user):
    """Attach the request's files to its posted payment: those not yet in SAP. `(count, problem)`.

    The payment had no attachment (it failed at posting): one is made and set
    on the payment. It had one: the new files are added to it as lines.
    """
    from advance_payment.services import flow as flow_service
    from advance_payment.services import voucher as voucher_service
    from payments import sap_client

    if not enabled():
        return 0, 'The file upload service is not configured (FILE_UPLOAD_URL / FILE_UPLOAD_TOKEN).'
    voucher = voucher_service.live(advance)
    if voucher is None or voucher.status != VoucherStatus.POSTED or not voucher.sap_doc_entry:
        return 0, 'The payment is not posted to SAP yet: its files are attached when it posts.'
    ready, missing = _ready(advance)
    todo = [r for r in ready if r.sap_attachment_entry != voucher.attachment_entry or not voucher.attachment_entry]
    if not todo:
        return 0, ('Not on the share yet: ' + '; '.join(missing)) if missing else ''
    company_db = voucher_service._company_db(advance)
    source = _source_path(advance.company)
    try:
        if voucher.attachment_entry:
            try:
                sap_client.request('PATCH', f'/Attachments2({voucher.attachment_entry})', company_db=company_db,
                                   json_body={'Attachments2_Lines': [_line(r, source) for r in todo]})
            except sap_client.SapError as exc:
                if not (_folder_refused(exc) and sap_attachment_rows.enabled()):
                    raise
                sap_attachment_rows.add(company_db, voucher.attachment_entry, _files(todo))
            RequestFile.objects.filter(pk__in=[r.pk for r in todo]).update(
                sap_attachment_entry=voucher.attachment_entry)
        else:
            entry, problem = _new_attachment(todo, advance.company, company_db)
            if problem:
                voucher.attachment_error = problem[:500]
                voucher.save(update_fields=['attachment_error'])
                return 0, problem
            # The payment, or (an Expense to a vendor) the A/P invoice it booked.
            invoice = getattr(voucher, 'sap_object', '') == VoucherObject.PURCHASE_INVOICE
            target = 'PurchaseInvoices' if invoice else 'VendorPayments'
            try:
                sap_client.request('PATCH', f'/{target}({int(voucher.sap_doc_entry)})',
                                   company_db=company_db, json_body={'AttachmentEntry': entry})
            except sap_client.SapError as exc:
                # Any refusal, not only the share's: a PATCH of a posted payment
                # runs SAP's posting rules again (after 5 PM: "(4612) Urgency
                # field is mandatory"), which an attachment is not subject to.
                # Only the attachment number is set, and only where there is none.
                if exc.status_code is None or invoice or not sap_attachment_rows.enabled():
                    raise
                sap_attachment_rows.link_payment(company_db, int(voucher.sap_doc_entry), entry)
            voucher.attachment_entry = entry
    except sap_attachment_rows.RowsError as exc:
        problem = f'SAP did not take the attachment: {exc}'
        voucher.attachment_error = problem[:500]
        voucher.save(update_fields=['attachment_error'])
        return 0, problem
    except sap_client.SapError as exc:
        problem = _sap_problem(exc)
        voucher.attachment_error = problem[:500]
        voucher.save(update_fields=['attachment_error'])
        return 0, problem
    voucher.attachment_error = ('Not on the share yet: ' + '; '.join(missing))[:500] if missing else ''
    voucher.save(update_fields=['attachment_entry', 'attachment_error'])
    flow_service.log(advance, LogAction.SAP_ATTACHED, user=user, stage=None,
                     data={'attachment': voucher.attachment_entry, 'files': [r.name for r in todo]})
    return len(todo), ''
