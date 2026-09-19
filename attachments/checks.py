"""Startup validation of the attachment storage paths.

THE FAILURE THIS CATCHES. `PAYMENTS_IMAGES` was once set to

    PS C:\\Users\\JIVO\\Desktop\\PROJECT\\OMS-Backend\\media\\PaymentAttachments\\Recieve

— a PowerShell prompt (`PS C:\\>`) copied into `.env` along with the path. The
value looks right at a glance, and nothing complained until somebody
photographed a cheque: `save_upload` called `os.makedirs`, Windows rejected a
directory named `PS C:` with WinError 123, and the upload view's catch-all
turned that into a 502 "The file store is currently unavailable." The mobile
client then swallowed the failure into a note appended to the success message,
so the receipt saved and the attachment silently did not.

Four uploads failed that way before anyone noticed, and the only record was a
traceback in `oms_errors.log`.

A system check moves that discovery to `manage.py check` and to server start —
before a user has photographed anything — and names the setting, its value and
what is wrong with it.

WHY `isabs` IS THE WHOLE TEST. Every realistic corruption of a pasted path
fails it and every valid form passes:

    C:\\Users\\...\\Recieve          -> ntpath.isabs  True   accepted
    \\\\JIVO-APP\\Payments\\Receive    -> ntpath.isabs  True   accepted (UNC)
    /srv/payments/receive          -> posixpath.isabs True  accepted (POSIX)
    PS C:\\Users\\...                -> False                 THE BUG
    "C:\\Users\\..."                 -> False                 quoted
    ␣␣C:\\Users\\...␣␣               -> False                 padded
    media\\PaymentAttachments       -> False                 relative

BOTH path flavours are tried, never `os.path`, because the server may run on
Linux while the share is a Windows UNC path — `storage.save_upload` already
splits on exactly that distinction. Using `os.path.isabs` would reject every
UNC path the moment this ran on POSIX.
"""
import ntpath
import posixpath

from django.conf import settings
from django.core.checks import Error, Warning as CheckWarning, register

#: setting name -> what it stores, for the message.
STORAGE_SETTINGS = {
    'PAYMENTS_IMAGES': 'cheque images and UPI screenshots',
    'DEPOSIT_PAYMENTS_IMAGES': 'deposit slips and bank receipts',
}


def looks_absolute(value):
    """True for a Windows path, a UNC share or a POSIX path. See module doc."""
    return ntpath.isabs(value) or posixpath.isabs(value)


@register()
def check_attachment_storage(app_configs, **kwargs):
    """`manage.py check`, and therefore every `runserver`/`migrate`."""
    problems = []

    for name, holds in STORAGE_SETTINGS.items():
        raw = getattr(settings, name, '')
        value = '' if raw is None else str(raw)

        if not value.strip():
            # A WARNING, not an ERROR: a deployment that never receives
            # attachments is legitimate, and refusing to boot over it would be
            # worse than the uploads failing. `directory_for` still rejects the
            # upload with a clear message if one is attempted.
            problems.append(CheckWarning(
                f'{name} is not set, so uploading {holds} will fail.',
                hint=f'Set {name} in .env to the folder or share that stores '
                     f'them.',
                id='attachments.W001',
            ))
            continue

        if value != value.strip():
            problems.append(Error(
                f'{name} has leading or trailing whitespace: {value!r}.',
                hint='Remove the spaces. The path is used verbatim, so they '
                     'become part of the directory name.',
                id='attachments.E001',
            ))
            continue

        if not looks_absolute(value):
            # The `PS C:` case lands here, and so does a quoted or relative
            # path. Quote the value in the message: the whole point is that
            # this is invisible when read casually.
            problems.append(Error(
                f'{name} is not an absolute path: {value!r}.',
                hint='Expected a drive path (C:\\folder), a UNC share '
                     '(\\\\SERVER\\share) or a POSIX path (/srv/folder). A '
                     'value beginning "PS " is a PowerShell prompt copied in '
                     'with the path; delete the prompt. Surrounding quotes '
                     'must be removed too.',
                id='attachments.E002',
            ))

    return problems
