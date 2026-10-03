"""The Control Panel's template context, only on Control Panel pages.

C_Panel's two context processors (`user_profile`: the `can_*` flags, the user
and the ticker; `ui_mode`: the shell to extend) ran on every page of a
Control-Panel-only site. Inside OMS the same TEMPLATES setting also renders
Django admin, so they are wrapped to do nothing outside C_Panel's own paths.
"""
from control_panel.permissions import INVENTORY_TABS, OILS_TABS, TARGETS, allowed_tabs
from core.permissions import effective_keys
from cpanel.core.context_processors import user_profile
from cpanel.core.ui_mode import ui_mode

#: Where C_Panel's server-rendered pages live (see cpanel/urls.py).
PAGE_PREFIXES = ('/realise/', '/sales/', '/inventory/', '/expenses/', '/salaries/', '/cp/')


def cpanel_context(request):
    if not request.path.startswith(PAGE_PREFIXES):
        return {}
    user = request.user
    return {
        **user_profile(request),
        **ui_mode(request),
        # The sub-tabs of Oils Sale and Inventory this user holds — the pages
        # show only these (control_panel/permissions.py).
        'cp_oils_tabs': allowed_tabs(user, OILS_TABS),
        'cp_inventory_tabs': allowed_tabs(user, INVENTORY_TABS),
        'cp_can_targets': TARGETS in effective_keys(user),
    }
