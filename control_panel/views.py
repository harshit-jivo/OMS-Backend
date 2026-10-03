"""The Control Panel's one JSON endpoint: the link that opens a page.

    POST /api/control-panel/sso/   {"page": "oils-sale"}
    -> {success, data: {path}}     path is on this backend: /cp/session/?ticket=...

    400  unknown page          403  no key for that page

POST rather than GET so the single-use link is never cached or prefetched.
"""
from rest_framework import status as http_status
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from core.responses import fail, ok
from control_panel import permissions as perms
from control_panel import sso


class SsoLinkView(APIView):

    def get_permissions(self):
        return [IsAuthenticated(), perms.CanOpenAnyControlPanelPage()]

    def post(self, request):
        page = str((request.data or {}).get('page') or '').strip()
        if page not in perms.PAGES:
            return fail(f'Unknown Control Panel page {page!r}.')
        try:
            path = sso.page_link(request.user, page)
        except sso.NotAllowed as exc:
            return fail(str(exc), status=http_status.HTTP_403_FORBIDDEN)
        return ok({'path': path})
