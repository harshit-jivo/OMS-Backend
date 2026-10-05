"""Refuse a Control Panel page to a user without its OMS key.

C_Panel gates by group, and one group opens several pages: Oils Sale and every
Sales sub-page ride on the Realise group. OMS grants them as two separate pages
(Oils Sale, Sales), so an Oils-Sale-only user holds that group too — and could
otherwise type /realise/targets/ into a tab. This checks the page path against
the key that opens it (control_panel.permissions.PATH_KEYS) before the view runs.

Page paths only: the JSON endpoints behind them are shared between pages of
the same group, and C_Panel's group checks keep guarding those.
"""
from django.shortcuts import render

from control_panel.permissions import PATH_KEYS
from core.permissions import effective_keys


class ControlPanelPageGuard:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        key = PATH_KEYS.get(request.path)
        user = getattr(request, 'user', None)
        # Not signed in: the view's own login check sends them to /cp/signed-out/.
        if key and user is not None and user.is_authenticated and key not in effective_keys(user):
            return render(request, 'core/oms_signed_out.html',
                          {'reason': 'You do not have access to this page.'}, status=403)
        return self.get_response(request)
