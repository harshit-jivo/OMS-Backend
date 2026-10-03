"""Let the OMS web app frame the Control Panel pages — and nothing else.

The OMS app shows each Control Panel page in a frame (its global styles and
scripts must not leak into the app). Django's XFrameOptionsMiddleware sends
`X-Frame-Options: DENY` on every response, which would refuse that frame even
from the same origin. For Control Panel page paths only, this replaces it with
CSP `frame-ancestors`: this backend itself ('self' — the Targets page frames
its own React app) plus the OMS web app's origins, which are exactly the
origins OMS already trusts for its API (CORS_ALLOWED_ORIGINS). Every other
response — the API, admin — keeps DENY.

Must sit AFTER XFrameOptionsMiddleware in MIDDLEWARE: responses travel back up
the list, so this runs first and the exemption is set by the time that
middleware looks for it.
"""
from django.conf import settings

from cpanel.core.context import PAGE_PREFIXES


class ControlPanelFramingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith(PAGE_PREFIXES):
            origins = ' '.join(getattr(settings, 'CORS_ALLOWED_ORIGINS', []) or [])
            response.xframe_options_exempt = True
            response['Content-Security-Policy'] = f"frame-ancestors 'self' {origins}".strip()
        return response
