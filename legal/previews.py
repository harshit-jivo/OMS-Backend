"""Where a check's artwork lives, and how it reaches the browser.

THE BUG THIS FIXES
------------------
The preview was handed to the page as a MEDIA URL — `/media/labels/previews/
x.png` — and `OMS/urls.py` serves `MEDIA_URL` only under `if settings.DEBUG`.
That is Django's own advice for `static()`, and it is correct advice; the
consequence is that the route simply does not exist in a deployed build. So
the checker worked on every developer's machine and the label was a broken
image icon on the server, which is where the compliance record is actually
read.

Serving media unconditionally would have been the small edit, and it is the
wrong one twice over: `django.views.static.serve` is documented as unfit for
production, and it would put every label PDF and every rendered preview behind
a guessable URL with NO permission check at all — for a module whose every
other endpoint is behind `LegalEndpointGate`. Pre-launch packaging artwork is
exactly the sort of thing that gate exists for. `attachments/views.py` already
made this call for cheque images and named the media route as the flaw it was
avoiding; this follows it rather than re-litigating it.

So the artwork is streamed by an authenticated view (`LabelPreviewView`) and
`image_url` names that view instead of a media path. This module is the piece
both sides share: the serializer asks "is there anything to show?" and the
view asks "what do I open?", and they must not be able to disagree — a
serializer that advertises a preview the view cannot find is a broken image
again, by a different route.

THE RESOLUTION ORDER
--------------------
Unchanged from the serializer this was lifted out of, because it encodes real
history rather than a preference:

1. the recorded preview (`preview_image`);
2. the path `service.save_preview` WOULD have written, when that file is still
   on disk — true for every check run before the field existed, since the
   previews were always generated and only the path went unrecorded;
3. the upload itself, but ONLY when it is already an image — a PDF in an
   `<img>` is a broken image, not a fallback;
4. nothing, and the page says so in words.

ABSOLUTE URLS PASS STRAIGHT THROUGH
-----------------------------------
If `default_storage` is ever a CDN or an object store, `preview_image` holds
an `https://…` URL. There is nothing for this server to open and no reason to
proxy the bytes, so such a URL is returned as-is and the browser fetches it
directly — the property `Label_Checker.tsx` already documents for `mediaUrl`.
"""
import os

from django.conf import settings
from django.core.files.storage import default_storage

from .service import IMAGE_SUFFIXES


def is_remote(url: str) -> bool:
    """Whether a resolved value is somewhere else's URL rather than a name here.

    Public because the streaming view needs the same answer, and two copies of
    "does this start with http" is exactly how the serializer and the view end
    up disagreeing about what a resolved value means.
    """
    return url.startswith('http://') or url.startswith('https://')


def _storage_name(url: str) -> str:
    """A stored URL (`/media/labels/x.png`) back to its name (`labels/x.png`).

    `preview_image` records what `default_storage.url()` returned, which is the
    only thing a browser could have used. Reading the file again means undoing
    that, and the prefix is `MEDIA_URL` — taken from settings rather than
    assumed to be `/media/`, since a deployment is free to change it.
    """
    prefix = settings.MEDIA_URL or ''
    return url[len(prefix):] if prefix and url.startswith(prefix) else url.lstrip('/')


def resolve(check) -> str:
    """The storage name of `check`'s displayable artwork, or ''.

    Returns an absolute URL unchanged (see the module note); everything else
    is a name `default_storage` can open, and it is only returned when the
    file is confirmed to exist — the point of this module is that the
    serializer never advertises what the view cannot serve.
    """
    recorded = (check.preview_image or '').strip()
    if recorded:
        if is_remote(recorded):
            return recorded
        name = _storage_name(recorded)
        if name and default_storage.exists(name):
            return name

    uploaded = check.label_file.name if check.label_file else ''
    if not uploaded:
        return ''

    stem = os.path.splitext(os.path.basename(uploaded))[0]
    derived = f'labels/previews/{stem}.png'
    if default_storage.exists(derived):
        return derived

    if (os.path.splitext(uploaded)[1].lower() in IMAGE_SUFFIXES
            and default_storage.exists(uploaded)):
        return uploaded

    return ''


def image_url(check) -> str:
    """What the API hands the page as `image_url`, or ''.

    A path under `/api/`, NOT `/media/` — the client turns it into a real URL
    with `resolveApiUrl`, so the version prefix the deployment is configured
    for is applied rather than baked in here.

    '' is a MEANINGFUL answer and not an error: the page has its own wording
    for a check that predates stored previews, which is a better thing to show
    a reviewer than a broken image icon.
    """
    resolved = resolve(check)
    if not resolved:
        return ''
    if is_remote(resolved):
        return resolved
    return f'/api/legal/history/{check.pk}/preview/'
