"""The link the OMS app opens a Control Panel page with.

The Control Panel pages are C_Panel's production pages, now part of OMS
(cpanel/): server-rendered by this backend, with their own scripts calling
their own APIs on a cookie session. The OMS app holds a JWT, not a session, so
it asks for a single-use ticket and opens

    /cp/session/?ticket=<signed>

which signs the SAME OMS user in and opens the page (cpanel/core/oms_session.py).
No second user table, no password, no other server.
"""
from control_panel.permissions import PAGES, cp_keys
from cpanel.core import oms_session


class NotAllowed(Exception):
    """Unknown page, or the user holds none of its keys."""


def page_link(user, page):
    """The relative link (on this backend) that opens `page` for `user`."""
    if page not in PAGES:
        raise NotAllowed(f'Unknown Control Panel page {page!r}.')
    path, key = PAGES[page]
    if key not in cp_keys(user):
        raise NotAllowed('You do not have access to this Control Panel page.')
    return f'/cp/session/?ticket={oms_session.issue(user, path)}'
