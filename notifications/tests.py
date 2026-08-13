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
