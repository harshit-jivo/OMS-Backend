"""Phase 3.1 verification tests for the notifications app.

Phase 3.1 only establishes the application BOUNDARY: the app must load, own no
models yet, keep AutoField, and import no business module. Behaviour (models,
dispatcher, APIs) arrives in later phases and is deliberately out of scope here.

These are ``SimpleTestCase`` checks -- no database is created or touched, and no
notification data is read or written.

Run with the project's established test settings::

    python manage.py test notifications --settings=OMS.test_settings
"""

import ast
import pathlib

import notifications
from django.apps import apps
from django.test import SimpleTestCase

from notifications.apps import NotificationsConfig

# The notification framework must never depend on a business module -- the
# dependency direction is business-module -> notifications, never the reverse
# (architecture Rule 2 / Rule 19). Checked statically below.
BUSINESS_MODULES = {
    "orders",
    "payments",
    "approvals",
    "inventory",
    "invoices",
    "users",
}


class NotificationsAppLoadingTests(SimpleTestCase):
    def test_app_is_installed_and_loads(self):
        config = apps.get_app_config("notifications")
        self.assertIsInstance(config, NotificationsConfig)
        self.assertEqual(config.name, "notifications")

    def test_default_auto_field_is_autofield(self):
        # Must stay AutoField: the later SeparateDatabaseAndState move that
        # brings the three notification models into this app must emit no
        # ALTER TABLE on the live integer-PK tables. BigAutoField here would.
        config = apps.get_app_config("notifications")
        self.assertEqual(
            config.default_auto_field, "django.db.models.AutoField"
        )

    def test_app_defines_no_models_yet(self):
        # Phase 3.1 owns no models. They move here later via a state-only
        # migration; declaring any now would clash on User/Order related_names.
        config = apps.get_app_config("notifications")
        self.assertEqual(list(config.get_models()), [])


class NotificationsRegistryTests(SimpleTestCase):
    """Phase 3.2: the framework foundation (constants + registry)."""

    def setUp(self):
        # The registry is process-global; keep each test isolated.
        from notifications import registry

        self.addCleanup(registry.clear)
        registry.clear()

    def test_registry_imports_and_initialises(self):
        from notifications import registry  # noqa: F401

        # Import alone must not raise and must expose the registration API.
        self.assertTrue(hasattr(registry, "register"))
        self.assertTrue(hasattr(registry, "get_handler"))

    def test_registry_starts_empty(self):
        # The framework must start clean — no business module registered.
        from notifications import registry

        self.assertEqual(registry.registered_events(), frozenset())

    def test_register_and_lookup_roundtrip(self):
        from notifications import constants, registry

        def handler(*args, **kwargs):
            return None

        registry.register(constants.PAYMENT_APPROVED, handler)
        self.assertTrue(registry.is_registered(constants.PAYMENT_APPROVED))
        self.assertIs(registry.get_handler(constants.PAYMENT_APPROVED), handler)
        self.assertEqual(
            registry.registered_events(), frozenset({constants.PAYMENT_APPROVED})
        )

    def test_register_rejects_invalid_event_name(self):
        from notifications import registry

        with self.assertRaises(ValueError):
            registry.register("Order Approved", lambda: None)  # not machine-readable

    def test_register_rejects_non_callable_handler(self):
        from notifications import constants, registry

        with self.assertRaises(TypeError):
            registry.register(constants.ORDER_CREATED, "not-callable")

    def test_register_rejects_duplicate(self):
        from notifications import constants, registry

        registry.register(constants.ORDER_CREATED, lambda: None)
        with self.assertRaises(ValueError):
            registry.register(constants.ORDER_CREATED, lambda: None)

    def test_unregistered_lookup_is_none(self):
        from notifications import constants, registry

        self.assertIsNone(registry.get_handler(constants.DEPOSIT_RECEIVED))


class NotificationsEventNameContractTests(SimpleTestCase):
    """Phase 3.2: every shipped event name follows the naming convention."""

    def test_all_event_names_follow_convention(self):
        from notifications import constants

        self.assertTrue(constants.EVENT_NAMES, "EVENT_NAMES must not be empty")
        for name in constants.EVENT_NAMES:
            self.assertTrue(
                constants.is_valid_event_name(name),
                f"event name {name!r} violates the UPPERCASE_UNDERSCORE convention",
            )

    def test_convention_rejects_human_readable_titles(self):
        from notifications import constants

        for bad in ["Approved", "Pending", "Order Approved", "order_created", "", 123]:
            self.assertFalse(
                constants.is_valid_event_name(bad),
                f"{bad!r} should not be a valid event name",
            )


class NotificationsDependencyRuleTests(SimpleTestCase):
    """The app must not import any business module (static source scan)."""

    @staticmethod
    def _top_level_imports(path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                # Only absolute imports name another top-level app; relative
                # imports (level > 0) stay inside the notifications package.
                if node.module and node.level == 0:
                    names.add(node.module.split(".")[0])
        return names

    def test_notifications_imports_no_business_module(self):
        pkg_dir = pathlib.Path(notifications.__file__).parent
        offenders = {}
        for py in pkg_dir.rglob("*.py"):
            if "__pycache__" in py.parts or py.name == "tests.py":
                continue  # this test file legitimately names the modules
            bad = self._top_level_imports(py) & BUSINESS_MODULES
            if bad:
                offenders[str(py.relative_to(pkg_dir))] = sorted(bad)
        self.assertEqual(
            offenders,
            {},
            f"notifications must not import business modules: {offenders}",
        )
