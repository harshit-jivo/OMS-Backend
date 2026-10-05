"""Single-use tickets: an OMS app user (JWT) -> a Control Panel page session.

The OMS web app authenticates with JWTs in the browser; the Control Panel pages
are server-rendered Django templates whose own scripts call their APIs with a
cookie session and CSRF token, exactly as in C_Panel. So the OMS app asks the
backend for a ticket (`control_panel/views.py`, JWT-authenticated), and the
page frame opens  /cp/session/?ticket=...  which signs the SAME user in.

A ticket is Django `signing` under SECRET_KEY, valid TICKET_MAX_AGE seconds,
names one page path from ALLOWED_PATHS, and is refused if seen before
(Django's cache; the 60-second lifetime bounds it across processes).
"""
import uuid

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.cache import cache

SALT = 'oms-cpanel-session'
TICKET_MAX_AGE = 60
#: A working day. The OMS app asks for a fresh ticket on every page open, so a
#: revoked key takes effect at the next open; the session should not outlive it.
SESSION_AGE = 8 * 3600

#: The only pages a ticket may open — the ones OMS shows.
ALLOWED_PATHS = (
    '/realise/', '/realise/beverages/', '/realise/realise-dashboard/', '/realise/targets/',
    '/realise/sales-channel/',
    '/sales/', '/inventory/', '/expenses/', '/salaries/',
    # C_Panel's report pages (control_panel.permissions.REPORTS).
    '/realise/compare-sales/', '/realise/sales-cn/', '/realise/hidden-sales/', '/realise/sales-flow/',
    '/realise/dispatch-details/', '/realise/realise-calculator/', '/realise/rate-list/',
    '/realise/plan-vs-done/', '/realise/customer-aging/', '/realise/beverages-gst/',
    '/realise/required-credit-limit/', '/realise/open-payments/', '/realise/claims/',
    '/inventory/reconciliation/', '/inventory/stock-available/', '/inventory/non-inventory/',
    '/realise/oih-vs-stock/', '/inventory/production/', '/inventory/daily-production/',
    '/realise/customer-master/',
)


class TicketRefused(Exception):
    pass


def issue(user, path):
    if path not in ALLOWED_PATHS:
        raise ValueError(f'Not a Control Panel page: {path!r}')
    return signing.dumps({'uid': user.pk, 'path': path, 'nonce': uuid.uuid4().hex}, salt=SALT)


def redeem(ticket):
    """`(user, path)` for a valid unused ticket, else TicketRefused."""
    try:
        data = signing.loads(ticket or '', salt=SALT, max_age=TICKET_MAX_AGE)
    except signing.SignatureExpired:
        raise TicketRefused('This link has expired. Reopen the page from OMS.') from None
    except signing.BadSignature:
        raise TicketRefused('This link is not valid. Reopen the page from OMS.') from None
    nonce = str(data.get('nonce') or '')
    if not nonce or not cache.add(f'cp-ticket:{nonce}', 1, timeout=TICKET_MAX_AGE * 5):
        raise TicketRefused('This link has already been used. Reopen the page from OMS.')
    path = data.get('path')
    if path not in ALLOWED_PATHS:
        raise TicketRefused('This link is not valid. Reopen the page from OMS.')
    user = get_user_model().objects.filter(pk=data.get('uid'), is_active=True).first()
    if user is None:
        raise TicketRefused('This account is not active.')
    return user, path
