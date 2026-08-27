"""Project-wide authorization tests.

Two things are asserted here that no per-app test can:

1. **The public allowlist.** Every route in the live URLconf is enumerated and
   compared against an explicit list of endpoints allowed to answer anonymous
   requests. A new open endpoint fails this test rather than shipping.
2. **The failure mode of a forgotten declaration.** `DEFAULT_PERMISSION_CLASSES`
   is `IsAuthenticated`, so a view that declares nothing is closed. Before that
   setting existed, DRF's own default (`AllowAny`) meant it was public — which
   is how 57 routes came to answer anonymous requests without anyone deciding.
3. **That `.env.example` is complete.** settings.py reads 18 keys with no
   default, and until now the example file documented none of them, so a fresh
   clone could not import settings at all.

This file lives in `core` rather than an app because the property it protects
belongs to the project: the routing table as a whole, not any one app's views.
"""
import ast
import inspect
import re
from pathlib import Path

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

#: Route prefixes that are not part of the API surface this audit covers.
#: Django's admin has its own authentication, and the static/media handlers
#: serve files by design.
#:
#: This replaced a filter that listed the project's own app modules and skipped
#: everything else — which meant a THIRD-PARTY view mounted in the URLconf was
#: invisible to the audit. That was not hypothetical: drf-spectacular's three
#: schema routes are AllowAny by default, and mounting them added three public
#: endpoints that this test, whose whole job is to notice public endpoints,
#: did not see. Auditing by route rather than by module has no such blind spot.
NON_API_PREFIXES = ('/admin/', '/static/', '/media/', '/api-auth/')


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
    """Every routable path on the API, whoever wrote the view."""
    for path, cb in _walk(get_resolver()):
        full = '/' + path
        if not full.startswith(NON_API_PREFIXES):
            yield full, cb


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


class EnvExampleTests(TestCase):
    """`.env.example` must document every setting that has no default.

    This is not documentation hygiene. A `config('X')` with no default raises
    `UndefinedValueError` at import, before Django's logging exists, so the
    symptom is a stack trace out of settings.py on a server that will not
    start — and the only way to learn which key is missing is to read
    settings.py. Seventeen keys were in that state when this test was written:
    the example file listed the twelve security settings added during this
    refactor and none of the connection settings the app has always required.

    settings.py is parsed as TEXT rather than imported, because importing it is
    exactly what fails when a key is missing.
    """

    #: Required, but must NOT appear with a value in the example file: these
    #: are generated secrets. Committing one is worse than omitting it —
    #: SECRET_KEY signs every JWT, and a shared VAPID private key lets anyone
    #: push notifications to this app's subscribers.
    GENERATED_SECRETS = {'SECRET_KEY', 'VAPID_PUBLIC_KEY', 'VAPID_PRIVATE_KEY'}

    def _example_path(self):
        from django.conf import settings as dj_settings

        path = Path(dj_settings.BASE_DIR) / '.env.example'
        self.assertTrue(path.exists(), '.env.example is missing')
        return path

    def _example_keys(self):
        """(assigned, mentioned-in-a-comment). Only the first kind survives a
        `cp .env.example .env`, which is why they are counted separately."""
        assigned, commented = set(), set()
        for line in self._example_path().read_text(encoding='utf-8').splitlines():
            stripped = line.strip()
            body = stripped.lstrip('#').strip()
            if '=' not in body:
                continue
            key = body.split('=', 1)[0].strip()
            if not key or not key.isupper() or not key.replace('_', '').isalnum():
                continue
            (commented if stripped.startswith('#') else assigned).add(key)
        return assigned, commented

    def test_every_key_without_a_default_is_documented(self):
        import OMS

        source = (Path(OMS.__file__).resolve().parent / 'settings.py').read_text(
            encoding='utf-8')
        # config('X') with no default argument — raises when the key is absent.
        required = set(re.findall(r"config\(\s*'([A-Z0-9_]+)'\s*\)", source))
        self.assertTrue(required, 'the settings.py scan matched nothing')

        assigned, commented = self._example_keys()
        undocumented = required - assigned - commented - self.GENERATED_SECRETS
        self.assertEqual(
            undocumented, set(),
            'settings.py requires these and .env.example never mentions them, '
            'so a fresh clone cannot start: ' + ', '.join(sorted(undocumented)))

    def test_a_required_key_is_assigned_not_merely_commented(self):
        """A commented `# DB_HOST=` documents the key but does not survive the
        copy, so the clone still fails to boot. Generated secrets are the
        deliberate exception."""
        import OMS

        source = (Path(OMS.__file__).resolve().parent / 'settings.py').read_text(
            encoding='utf-8')
        required = set(re.findall(r"config\(\s*'([A-Z0-9_]+)'\s*\)", source))
        assigned, _ = self._example_keys()
        missing = required - assigned - self.GENERATED_SECRETS
        self.assertEqual(
            missing, set(),
            'documented only as a comment, so `cp .env.example .env` still '
            'produces an unbootable file: ' + ', '.join(sorted(missing)))

    def test_generated_secrets_are_not_committed(self):
        """A placeholder value for these would be worse than nothing: the app
        boots, so nobody notices, and it runs on a key that is public in git."""
        assigned, _ = self._example_keys()
        leaked = assigned & self.GENERATED_SECRETS
        self.assertEqual(
            leaked - {'SECRET_KEY'}, set(),
            f'.env.example assigns a value to {sorted(leaked)}')

    def test_the_secret_key_entry_is_present_and_empty(self):
        """SECRET_KEY is the one secret that must still APPEAR, so the copied
        file has an obvious blank to fill rather than a key the reader has to
        already know about."""
        lines = [ln.strip()
                 for ln in self._example_path().read_text(encoding='utf-8').splitlines()]
        self.assertIn('SECRET_KEY=', lines,
                      'SECRET_KEY must be present and blank')


class NoCommittedCredentialsTests(TestCase):
    """No credential may have a value baked into the source.

    settings.py carried the production SAP and JSAP host, database, user and
    password as `config(..., default='<literal>')`, and the same values again
    as `getattr(settings, ..., '<literal>')` fallbacks in
    sap_sync/services/connection.py and invoice/services/jsap_db.py. The
    duplication is what made this dangerous rather than merely untidy: removing
    the settings defaults alone would have changed nothing, and clearing the
    .env would have silently reconnected to production instead of failing.

    No secret appears in this file. The tests look for the SHAPE of a committed
    credential — a key whose name says it is one, holding a non-empty literal —
    so they keep working after the exposed values are rotated, which writing
    the values down here would not.
    """

    #: A key is a credential if its name ends in one of these. USER is included
    #: because a service account name is half of a credential and names the
    #: privilege level; HOST and NAME are not — those are topology, and
    #: ALLOWED_HOSTS legitimately lists this server's own address.
    CREDENTIAL_SUFFIXES = ('PASSWORD', 'SECRET', 'TOKEN', 'KEY', 'USER')

    #: Not credentials despite matching the suffixes above. Kept as an
    #: explicit list rather than a cleverer rule: every entry here is a
    #: decision someone should have to write down.
    EXEMPT = {
        # A rate limit ("2000/hour"). Matches only because of the USER suffix.
        'THROTTLE_USER',
        'THROTTLE_ANON',
        'THROTTLE_LOGIN',
        # An integer id stamped on DSR requests, not a secret.
        'OMS_JSAP_USER_ID',
        # A contact address sent to push services, published by design.
        'VAPID_ADMIN_EMAIL',
    }

    def _settings_source(self):
        import OMS

        return (Path(OMS.__file__).resolve().parent / 'settings.py').read_text(
            encoding='utf-8')

    def test_no_credential_setting_has_a_committed_default(self):
        source = self._settings_source()
        # config('X', default='literal') / default="literal" — non-empty only.
        pattern = re.compile(
            r"config\(\s*'([A-Z0-9_]+)'\s*,\s*default\s*=\s*(['\"])([^'\"]+)\2")
        offenders = [
            key for key, _q, _value in pattern.findall(source)
            if key not in self.EXEMPT
            and key.endswith(self.CREDENTIAL_SUFFIXES)
        ]
        self.assertEqual(
            offenders, [],
            'these settings ship a working credential to anyone who can read '
            'the repo: ' + ', '.join(sorted(set(offenders))))

    def test_the_sap_connection_reads_settings_with_no_fallback(self):
        """`getattr(settings, 'SAP_DB_PASSWORD', '<literal>')` is the same leak
        one layer down, and it survives any cleanup of settings.py."""
        import invoice.services.jsap_db as jsap_db
        import sap_sync.services.connection as sap_connection

        pattern = re.compile(
            r"getattr\(\s*settings\s*,\s*'([A-Z0-9_]+)'\s*,\s*(['\"])([^'\"]+)\2")
        for module in (sap_connection, jsap_db):
            source = Path(module.__file__).read_text(encoding='utf-8')
            offenders = [key for key, _q, _v in pattern.findall(source)]
            self.assertEqual(
                offenders, [],
                f'{module.__name__} falls back to a hardcoded value for: '
                + ', '.join(sorted(set(offenders))))

    def test_the_sap_credentials_are_required_not_blank_defaulted(self):
        """A blank default would be quieter but wrong: sap_sync has no
        meaningful disabled mode, so an unset key should stop the process at
        startup, not surface later as a connection failure to ''."""
        source = self._settings_source()
        for key in ('SAP_DB_HOST', 'SAP_DB_NAME', 'SAP_DB_USER',
                    'SAP_DB_PASSWORD'):
            with self.subTest(setting=key):
                self.assertIn(
                    f"config('{key}')", source,
                    f'{key} should be read with no default at all')

    def test_jsap_is_configured_all_or_nothing(self):
        """JSAP is optional, so it is blank-defaulted rather than required —
        but a half-set block is a typo, and settings.py refuses to start on it
        rather than let `is_configured()` return true for a broken config."""
        source = self._settings_source()
        self.assertIn('if JSAP_DB_HOST and not (JSAP_DB_NAME', source)

    def test_settings_defines_each_credential_setting_exactly_once(self):
        """The JSAP block was defined TWICE. The second copy, 30 lines below
        the first, carried the real password and — being later in the file —
        silently won, making the documented "blank host disables JSAP" mode
        unreachable. A redefinition is invisible to every other test here,
        because the module imports cleanly and only the last value survives.
        """
        source = self._settings_source()
        for key in ('JSAP_DB_HOST', 'JSAP_DB_NAME', 'JSAP_DB_USER',
                    'JSAP_DB_PASSWORD', 'SAP_DB_HOST', 'SAP_DB_PASSWORD'):
            with self.subTest(setting=key):
                assignments = re.findall(
                    rf'^{key}\s*=', source, flags=re.MULTILINE)
                self.assertEqual(
                    len(assignments), 1,
                    f'{key} is assigned {len(assignments)} times; the last '
                    f'assignment silently wins')


class OpenAPISchemaTests(TestCase):
    """The generated OpenAPI schema (Phase 0.6).

    The value here is that the schema is derived from the code, so it cannot
    drift from it the way the hand-written documents in `docs/` did. It also
    answers Phase 0.5 — who may call what — because every operation carries the
    permission classes of the view behind it.
    """

    #: Views drf-spectacular cannot describe a request/response body for,
    #: because they subclass APIView and build their Response by hand rather
    #: than declaring a serializer. The path is still documented; only the body
    #: is missing.
    #:
    #: This is a CEILING, not an expected value. It fails only when the number
    #: GROWS — that is, when a new undescribed view is added — so the backlog
    #: can be worked down without touching this test, and the intended
    #: direction is down. Lower it as views gain a `serializer_class` or an
    #: `@extend_schema`.
    MAX_UNDESCRIBED_VIEWS = 297

    def _generate(self):
        from drf_spectacular.generators import SchemaGenerator

        return SchemaGenerator().get_schema(request=None, public=True)

    def test_the_schema_generates(self):
        """A view that breaks generation breaks it for every other view too —
        the command produces one document or none."""
        schema = self._generate()
        self.assertEqual(schema['openapi'][:2], '3.')
        self.assertGreater(len(schema['paths']), 200,
                           'far fewer paths than the URLconf has routes')

    def test_only_api_routes_are_described(self):
        """SCHEMA_PATH_PREFIX keeps the Django admin out of the API document."""
        schema = self._generate()
        stray = [p for p in schema['paths'] if not p.startswith('/api/')]
        self.assertEqual(stray, [], f'non-API paths in the schema: {stray}')

    def test_undescribed_views_do_not_increase(self):
        """See MAX_UNDESCRIBED_VIEWS. Counted from the schema rather than from
        the command's stderr, so it measures the artefact, not the log."""
        schema = self._generate()
        undescribed = 0
        for operations in schema['paths'].values():
            for method, operation in operations.items():
                if method not in {'get', 'post', 'put', 'patch', 'delete'}:
                    continue
                responses = operation.get('responses', {})
                if not any(r.get('content') for r in responses.values()):
                    undescribed += 1
        self.assertLessEqual(
            undescribed, self.MAX_UNDESCRIBED_VIEWS,
            f'{undescribed} operations have no described response body, up '
            f'from {self.MAX_UNDESCRIBED_VIEWS}. A new APIView needs a '
            f'serializer_class or an @extend_schema.')


class SchemaEndpointTests(TestCase):
    """The three schema routes must not be public.

    A schema is a complete map of the API — every endpoint, every field name,
    every enum. That is reconnaissance. These routes declare no permission
    classes, so they inherit DEFAULT_PERMISSION_CLASSES; this test is what
    notices if that ever stops being true.
    """

    ROUTES = ('/api/schema/', '/api/schema/swagger-ui/', '/api/schema/redoc/')

    def test_anonymous_access_is_refused(self):
        for route in self.ROUTES:
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertIn(
                    response.status_code, (401, 403),
                    f'{route} answered {response.status_code} to an anonymous '
                    f'request')

    def test_an_authenticated_user_can_read_the_schema(self):
        """Locked down, not switched off — the pair matters. A schema route
        that 403s for everyone would also pass the test above.

        APIClient rather than Django's test client: DEFAULT_AUTHENTICATION_
        CLASSES is JWT only, so a session login leaves DRF seeing an anonymous
        request and this would fail with a misleading 401.
        """
        from rest_framework.test import APIClient

        role, _ = UserRole.objects.get_or_create(name='sales')
        user = User.objects.create_user(
            username='schema-reader', password='pw-for-tests-only', role=role)
        client = APIClient()
        client.force_authenticate(user)
        response = client.get('/api/schema/')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'openapi', response.content[:200].lower())


class ModelTableOwnershipTests(TestCase):
    """No two models may map to the same database table.

    `orders.Branches` and `sap_sync.Branch` both mapped to `branches`, and the
    two disagreed about it: bpl_id was CharField(50) in one and IntegerField in
    the other, against a column that is `integer`. So the same 22 rows came
    back as JSON strings through /api/orders/branch/ and as numbers through
    every sap_sync endpoint, and neither model was obviously the wrong one to
    read from.

    Nothing catches this on its own. Both models were `managed = False`, so
    Django never issued DDL from either and never had cause to compare them;
    `makemigrations --check` is satisfied, the app imports, and the tests pass.
    It is only visible by looking for it, which is what this does.
    """

    def test_no_two_models_share_a_table(self):
        import collections

        from django.apps import apps

        by_table = collections.defaultdict(list)
        for model in apps.get_models():
            table = (model._meta.db_table or '').lower()
            label = f'{model._meta.app_label}.{model.__name__}'
            if not model._meta.managed:
                label += ' (unmanaged)'
            by_table[table].append(label)

        shared = {t: sorted(m) for t, m in by_table.items() if len(m) > 1}
        self.assertEqual(shared, {}, (
            'Two models on one table. Pick the one that matches the column '
            'types and delete the other; if both are genuinely needed, a proxy '
            'model (Meta.proxy = True) says so explicitly and shares the '
            'field definitions instead of duplicating them:\n  '
            + '\n  '.join(f'{t}: {", ".join(m)}' for t, m in sorted(shared.items()))
        ))


class UnresolvedNameTests(TestCase):
    """No module may use a name it never defines or imports.

    Python resolves globals when a line RUNS, not when the module is imported.
    So a function that references a name nothing provides imports cleanly,
    passes `manage.py check`, and raises NameError the first time a request
    reaches that line — in production, on whichever endpoint nobody tested.

    This is not hypothetical. Splitting `orders/views.py` and `users/views.py`
    into packages produced exactly this bug three times, and the 539-test suite
    was green for all three:

    * `logger` is a module-level ASSIGNMENT, so a header-copying split gave it
      to one module and left the others to raise on their first log line;
    * `_save_template_if_unique` moved to a service and its caller was never
      given the import — a NameError waiting on the next saved order template;
    * `_normalize_category` was placed in the wrong module, one level away from
      the helper that needed it.

    None of those is reachable by the tests, which is the point: this checks
    the code, not a code path.

    What it does NOT catch is a bad module PATH — `from .models import X` in a
    module that moved one package deeper resolves `X` fine as a name and fails
    at import. `manage.py check` catches that one, and this test does not
    replace it.
    """

    #: Every first-party app. Third-party packages are not ours to police.
    APPS = (
        'orders', 'users', 'sap_sync', 'hana', 'payments', 'notifications',
        'tracker', 'invoice', 'einvoice', 'ewaybill', 'devices', 'approvals',
        'attachments', 'core', 'legal', 'audit', 'uilabels', 'serviceLayer',
        'SKU', 'HAIS',
    )

    @staticmethod
    def _free_names(tree):
        """(unresolved names, whether the module uses `import *`).

        A star import makes the available names unknowable statically, so those
        modules are reported and skipped rather than guessed at.
        """
        import builtins

        defined = set(dir(builtins)) | {
            '__name__', '__doc__', '__file__', '__all__', '__path__'}
        star = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if alias.name == '*':
                        star = True
                    defined.add(alias.asname or alias.name.split('.')[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
            elif isinstance(node, ast.Global):
                defined.update(node.names)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        return used - defined, star

    def _sources(self):
        from django.conf import settings as dj_settings

        base = Path(dj_settings.BASE_DIR)
        for app in self.APPS:
            for path in sorted((base / app).rglob('*.py')):
                # Migrations are generated and frozen; __pycache__ is not source.
                if 'migrations' in path.parts or '__pycache__' in path.parts:
                    continue
                yield path

    def test_no_module_uses_an_undefined_name(self):
        offenders = {}
        scanned = 0
        for path in self._sources():
            free, star = self._free_names(
                ast.parse(path.read_text(encoding='utf-8')))
            if star:
                continue
            scanned += 1
            if free:
                offenders[str(path)] = sorted(free)

        self.assertGreater(scanned, 200,
                           'the scan matched almost nothing — check APPS')
        self.assertEqual(offenders, {}, (
            'these names are used but never defined or imported, which is a '
            'NameError the first time the line runs:\n  '
            + '\n  '.join(f'{p}: {", ".join(names)}'
                          for p, names in sorted(offenders.items()))))
