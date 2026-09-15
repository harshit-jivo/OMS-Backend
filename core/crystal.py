"""Talking to the Crystal Reports service.

Two OMS endpoints print through it — `invoice/crystal/` for A/R invoices and
`orders/crystal/` for sales orders — and the details of the service belong in
neither of them: the render timeout, how a failure comes back, and what the PDF
is called when the browser saves it are the same either way. What differs is
only the path a company maps to, which each view owns.

The service itself is documented in `Crystal Report Utility/README.md`. It has
no authentication and must stay on the internal network; `CRYSTAL_URL` in the
environment is the authority on where it lives. It addresses documents by
`DocEntry`, never `DocNum` — see `hana.utils.resolve_doc_entry` and
`resolve_order_doc_entry` for why that distinction matters per company.
"""
import re
from urllib.parse import quote

import requests
from django.conf import settings
from django.http import HttpResponse
from rest_framework import status
from rest_framework.response import Response

# Characters Windows/macOS refuse in a filename, plus control chars.
_BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')

# One request renders a whole report: Crystal loads the .rpt, opens its ODBC
# connection and runs a stored procedure for the document plus one per
# subreport. The first call after an IIS app-pool recycle additionally waits for
# the Crystal runtime to warm up, which is why this is generous.
RENDER_TIMEOUT_SECONDS = 60


def _verify():
    """TLS verification for the Crystal call.

    Mirrors `invoice.views._external_verify`, for the same reason: the service
    is plain http today, where requests ignores `verify` entirely — so this is
    not a live exposure, it is what stops the day `CRYSTAL_URL` gains an `s`
    from silently verifying nothing.
    """
    return getattr(settings, 'EXTERNAL_SSL_VERIFY', True)


def download_name(doc_num, party_name):
    """'<DocNum> <Party Name>.pdf', scrubbed so it is a legal filename.

    The party name is whatever the caller passed, so it is sanitised rather
    than trusted: illegal characters out, whitespace collapsed, and the
    length capped well inside the 255-byte filesystem limit.
    """
    party = _BAD_FILENAME_CHARS.sub(' ', str(party_name or ''))
    party = ' '.join(party.split())[:120].strip(' .')
    stem = f"{doc_num} {party}".strip() if party else str(doc_num)
    return f"{stem}.pdf"


def render_pdf(path, doc_entry, filename, ascii_fallback='report.pdf'):
    """GET `{CRYSTAL_URL}/{path}/{doc_entry}` and wrap the PDF for a browser.

    `path` is the company-specific route on the service ('api/salesorder/bev'),
    which is what picks the ODBC DSN and HANA schema the report is read through.

    Returns an `HttpResponse` carrying the PDF on success, or a DRF `Response`
    carrying the error — the caller returns whichever it gets, which is what
    keeps both print views down to their own argument checking.
    """
    url = f"{settings.CRYSTAL_URL}/{path}/{doc_entry}"
    try:
        crystal_response = requests.get(
            url, timeout=RENDER_TIMEOUT_SECONDS, verify=_verify())
    except requests.RequestException as exc:
        return Response({'error': str(exc)}, status=status.HTTP_502_BAD_GATEWAY)

    if not crystal_response.ok:
        return Response({'error': 'Failed to generate print report',
                         'details': crystal_response.text},
                        status=crystal_response.status_code)

    resp = HttpResponse(
        crystal_response.content,
        status=crystal_response.status_code,
        content_type=crystal_response.headers.get('Content-Type', 'application/pdf'),
    )
    # Shown inline in the browser's PDF viewer, but this is also the name the
    # viewer's Download button uses. The RFC 5987 filename* carries names with
    # non-ASCII characters; the plain filename is the ASCII fallback for older
    # clients, and `ascii_fallback` covers a name with no ASCII left in it at all.
    ascii_name = filename.encode('ascii', 'ignore').decode() or ascii_fallback
    resp['Content-Disposition'] = (
        f'inline; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )
    resp.xframe_options_exempt = True
    return resp
