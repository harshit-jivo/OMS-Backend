"""Request-level audit capture.

Identifies who is making each admin-page change (from the JWT) and shares that
with the model signals. If a recognised admin change happened but no model
signal logged it (e.g. a bulk ``queryset.update()`` or an endpoint whose model
isn't individually audited), the middleware writes one simple fallback row so
the change is never lost.
"""
import logging

from . import context, signals
from .pages import resolve_page

logger = logging.getLogger(__name__)

MUTATING_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}

_ACTION_BY_METHOD = {
    'POST': 'Created',
    'PUT': 'Updated',
    'PATCH': 'Updated',
    'DELETE': 'Deleted',
}


def _resolve_user(request):
    """Identify the user from the JWT (works even on AllowAny views)."""
    try:
        from rest_framework_simplejwt.authentication import JWTAuthentication
        result = JWTAuthentication().authenticate(request)
        if result is not None:
            return result[0]
    except Exception:
        pass
    user = getattr(request, 'user', None)
    if user is not None and getattr(user, 'is_authenticated', False):
        return user
    return None


class AuditMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        is_admin_change = request.method in MUTATING_METHODS
        page = resolve_page(request.path) if is_admin_change else ''
        user = _resolve_user(request) if is_admin_change else None

        context.begin(user=user, page=page)

        response = None
        try:
            response = self.get_response(request)
        finally:
            written = 0
            try:
                written = signals.flush()  # write one row per changed record
            except Exception:
                logger.exception('Failed to flush audit buffer')
            self._maybe_fallback(request, response, page, user, written)
            context.clear()

        return response

    def _maybe_fallback(self, request, response, page, user, written):
        # Only for recognised admin changes that succeeded but produced no
        # model-signal row (e.g. a bulk queryset.update() or an endpoint whose
        # model isn't individually audited).
        if not page or written > 0:
            return
        status = getattr(response, 'status_code', None)
        if status is not None and status >= 400:
            return

        from .models import AuditLog
        action = _ACTION_BY_METHOD.get(request.method, 'Changed')
        try:
            AuditLog.objects.create(
                user=user if getattr(user, 'pk', None) else None,
                username=getattr(user, 'username', '') or '',
                page=page,
                action=action,
                record=page,
            )
        except Exception:
            logger.exception('Failed to write fallback audit log')
