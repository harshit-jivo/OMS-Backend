"""The Control Panel's own views inside OMS: the sign-in handshake, the
signed-out page, the top-strip ticker, and placeholders for C_Panel routes
that OMS replaces.

WHAT IS NOT HERE, ON PURPOSE
----------------------------
C_Panel's login view (it created an account for any failed login), its
look-switcher, its logout and its User Management screen (which could create
users and make superusers). Inside OMS, users, sign-in and access are OMS's:
the routes still exist by name — the templates link to them — but answer with
a redirect to OMS or a 404 (see `placeholder`).
"""
from django.contrib.auth import login
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .context_processors import (
    _filter_ticker_items,
    _resolve_period,
    build_user_permissions,
    get_ticker_items,
)
from . import oms_session


def nav_ticker(request):
    """Top-strip numbers, fetched by the browser after the page is on screen."""
    if not request.user.is_authenticated:
        return JsonResponse({'error': 'Authentication required'}, status=401)
    year, month = _resolve_period(request)
    items = get_ticker_items(year, month, blocking=True)
    items = _filter_ticker_items(items, build_user_permissions(request.user))
    return JsonResponse({'items': items})


@never_cache
@require_GET
def session_from_oms(request):
    """`/cp/session/?ticket=...` — the OMS app's single-use ticket becomes a
    session for the SAME OMS user, then the page it names opens.

    The OMS app works with JWTs; these pages are server-rendered and need a
    cookie session. The ticket (oms_session.py) bridges the two without a
    password and without a second user table.
    """
    try:
        user, next_path = oms_session.redeem(request.GET.get('ticket', ''))
    except oms_session.TicketRefused as exc:
        return render(request, 'core/oms_signed_out.html', {'reason': str(exc)}, status=403)
    login(request, user, backend='django.contrib.auth.backends.ModelBackend')
    request.session.set_expiry(oms_session.SESSION_AGE)
    return redirect(next_path)


def signed_out(request):
    """Where a page sends a visitor without a session."""
    return render(request, 'core/oms_signed_out.html', {
        'reason': 'Your Control Panel session has ended.',
    })


def placeholder(request, *args, **kwargs):
    """A C_Panel route OMS does not serve (its login, logout, look switcher,
    user management). Navigations go back to the OMS app; anything else 404s."""
    if request.method == 'GET' and 'text/html' in request.headers.get('accept', ''):
        return redirect('signed_out')
    raise Http404('Not available inside OMS.')


def health(request):
    return HttpResponse('ok')
