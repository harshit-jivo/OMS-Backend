"""Project-wide authorization tests.

Two things are asserted here that no per-app test can:

1. **The public allowlist.** Every route in the live URLconf is enumerated and
   compared against an explicit list of endpoints allowed to answer anonymous
   requests. A new open endpoint fails this test rather than shipping.
2. **The failure mode of a forgotten declaration.** `DEFAULT_PERMISSION_CLASSES`
   is `IsAuthenticated`, so a view that declares nothing is closed. Before that
   setting existed, DRF's own default (`AllowAny`) meant it was public — which
   is how 57 routes came to answer anonymous requests without anyone deciding.

This file lives in `core` rather than an app because the property it protects
belongs to the project: the routing table as a whole, not any one app's views.
"""
import inspect

from django.test import TestCase
from django.urls import get_resolver
from django.urls.resolvers import URLPattern, URLResolver
from rest_framework.settings import api_settings

from core.permissions import IsAdminRole, is_admin
from users.models import User, UserRole

# ---------------------------------------------------------------------------
# The allowlist.
#
# Adding an entry here is a security decision. It says: this endpoint answers
# anyone on the internet, and that is intended. Each one carries the reason.
# ---------------------------------------------------------------------------

PUBLIC_ROUTES = {
    # Credentials are exchanged here, so there is nothing to authenticate with
    # yet. Throttled by the `login` scope instead — see users.views.LoginView.
    '/api/auth/login/',

    # The credential is the refresh token in the request body. Requiring a
    # bearer token would make this unusable exactly when it is needed: after
    # the access token expired.
    '/api/auth/refresh/',

    # Rendered by the browser as `<img src>`, in a print popup, and through
    # `window.open` — none of which can attach an Authorization header, and the
    # API issues no session cookie to fall back on. The content is the NIC
    # signed QR that is printed on the invoice and handed to the customer, and
    # the URL is keyed by a 64-character NIC hash, so it is neither secret nor
    # enumerable. Full reasoning in einvoice.views.irn_qr_png.
    #
    # Removing this entry requires OMS-Frontend's QrViewer to fetch through
    # axios and render an object URL first.
    '/api/einvoice/irn/<str:irn>/qr.png',
}

OPEN_PERMISSION_NAMES = {'AllowAny'}

LOCAL_APP_PREFIXES = (
    'users', 'orders', 'sap_sync', 'hana', 'SKU', 'serviceLayer', 'einvoice',
    'ewaybill', 'invoice', 'legal', 'audit', 'devices', 'tracker', 'uilabels',
    'core', 'approvals', 'attachments', 'payments', 'notifications', 'HAIS',
)


def _walk(resolver, prefix=''):
    for p in resolver.url_patterns:
        if isinstance(p, URLResolver):
            yield from _walk(p, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield prefix + str(p.pattern), p.callback


def _permission_names(cb):
    """The permission classes that actually apply, or None for a plain view."""
    cls = getattr(cb, 'view_class', None) or getattr(cb, 'cls', None)
    if cls is not None:
        return [c.__name__ for c in getattr(cls, 'permission_classes', [])]
    if hasattr(cb, 'initkwargs'):
        perms = (cb.initkwargs or {}).get('permission_classes')
        if perms is None:
            perms = api_settings.DEFAULT_PERMISSION_CLASSES
        return [c.__name__ for c in perms]
    # A plain Django view: never enters DRF dispatch, so no permission class
    # applies and none CAN be made to apply without converting it.
    return None


def _local_routes():
    for path, cb in _walk(get_resolver()):
        mod = getattr(cb, '__module__', '') or ''
        if mod.startswith(LOCAL_APP_PREFIXES):
            yield '/' + path, cb


class PublicEndpointAllowlistTests(TestCase):

    def test_only_allowlisted_routes_are_public(self):
        """The regression test for the whole permission phase.

        135 of 294 routes answered anonymous requests before it — HANA master
        data, e-invoice generation (which costs money and consumes a NIC rate
        limit), SAP sync triggers, order approval, scheme editing. Almost none
        of that was a decision; it was DRF's default applying to views that
        declared nothing.
        """
        unexpected = sorted(
            path for path, cb in _local_routes()
            if (names := _permission_names(cb)) is not None
            and names and all(n in OPEN_PERMISSION_NAMES for n in names)
            and path not in PUBLIC_ROUTES
        )
        self.assertEqual(unexpected, [], (
            'These routes answer unauthenticated requests and are not on the '
            'allowlist. Either give them a permission class, or add them to '
            'PUBLIC_ROUTES with the reason:\n  ' + '\n  '.join(unexpected)
        ))

    def test_no_plain_django_views_remain_on_the_api(self):
        """A view without `@api_view` never enters DRF's dispatch, so no
        permission class — not even DEFAULT_PERMISSION_CLASSES — can reach it.

        Two existed: `einvoice.irn_qr_png` and `orders.ai_order_summary`, the
        latter also `@csrf_exempt`. Both were anonymous by accident and both
        were invisible to any audit that greps for `permission_classes`,
        because there was nothing to grep for. This is the test that makes that
        category visible.
        """
        plain = sorted(
            path for path, cb in _local_routes()
            if _permission_names(cb) is None
            # Django's own admin and static/media handlers are not API routes.
            and not path.startswith(('/admin/', '/static/', '/media/'))
        )
        self.assertEqual(plain, [], (
            'Plain Django views on the API — convert to @api_view/APIView so a '
            'permission class can apply:\n  ' + '\n  '.join(plain)
        ))

    def test_the_default_is_closed(self):
        """Everything above rests on this. If the default reverts to AllowAny,
        every view that declares nothing silently reopens."""
        names = [c.__name__ for c in api_settings.DEFAULT_PERMISSION_CLASSES]
        self.assertEqual(names, ['IsAuthenticated'])

    def test_a_view_declaring_nothing_is_closed(self):
        """The property the setting is supposed to buy, verified end to end
        rather than by reading the setting back."""
        from rest_framework.views import APIView

        class ForgotToDeclare(APIView):
            pass

        self.assertEqual(
            [c.__name__ for c in ForgotToDeclare.permission_classes],
            ['IsAuthenticated'],
        )

    def test_every_allowlisted_route_still_exists(self):
        """Stops the allowlist rotting into a set of stale paths that quietly
        permit nothing — or, worse, permit a future route that reuses the URL.
        """
        live = {path for path, _ in _local_routes()}
        self.assertEqual(PUBLIC_ROUTES - live, set())


class AdminGuardTests(TestCase):
    """The SAP sync triggers rewrite the product and party masters that every
    order is priced from. All eight were AllowAny."""

    SYNC_VIEWS = [
        'SyncAllView', 'SyncProductsView', 'SyncPartiesView',
        'SyncPartyAddressesView', 'SyncBranchesView',
        'SyncScheduleListView', 'SyncScheduleDetailView', 'ToggleScheduleView',
    ]

    def test_sync_triggers_require_an_administrator(self):
        from sap_sync import views

        for name in self.SYNC_VIEWS:
            cls = getattr(views, name)
            perms = [c.__name__ for c in cls.permission_classes]
            self.assertIn(IsAdminRole.__name__, perms, name)

    def test_sync_view_docstrings_survived_the_edit(self):
        """The guard was inserted programmatically. Placed before a docstring
        instead of after it, the docstring becomes a bare expression and
        `__doc__` silently becomes None — a real outcome of the first attempt.
        """
        from sap_sync import views

        for name in self.SYNC_VIEWS:
            self.assertTrue(inspect.getdoc(getattr(views, name)), name)


class AdminDefinitionTests(TestCase):
    """`core.permissions.is_admin` replaced five disagreeing definitions.

    These assert the two widenings that adopting it caused, because both are
    deliberate and either could otherwise be mistaken for a bug later.
    """

    def _user(self, username, role_name=None, **kw):
        role = None
        if role_name:
            role, _ = UserRole.objects.get_or_create(
                name=role_name, defaults={'display_name': role_name})
        return User.objects.create_user(
            username=username, password='pw', name=username, role=role, **kw)

    def test_admin_via_extra_roles_now_counts(self):
        """All five previous definitions read `User.role` alone, while
        users/models.py states that every role check must consult `extra_roles`
        too. A manager granted `admin` as an extra role was refused everywhere.
        """
        user = self._user('c-dual', 'manager')
        role, _ = UserRole.objects.get_or_create(
            name='admin', defaults={'display_name': 'Admin'})
        user.extra_roles.add(role)
        self.assertTrue(is_admin(user))

    def test_staff_now_counts_everywhere(self):
        """`approvals` and `payments` honoured `is_staff`; `devices`,
        `uilabels` and `users` did not. One rule now."""
        self.assertTrue(is_admin(self._user('c-staff', None, is_staff=True)))

    def test_a_plain_user_is_not_an_admin(self):
        self.assertFalse(is_admin(self._user('c-plain', 'salesman')))
