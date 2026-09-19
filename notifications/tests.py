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
from unittest import mock

import notifications
from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import SimpleTestCase, TestCase, override_settings


# Module-level fake resolvers referenced by dotted path in override_settings
# (Phase 3.6 seam tests). They stand in for the real orders-side resolver so
# these tests import no business module.
def _fake_mobile_resolver(user):
    return ["ExponentPushToken[SEAM]"]


def _fake_web_resolver(user):
    return [{"endpoint": "https://push.example/seam", "keys": {"p256dh": "a", "auth": "b"}}]


def _boom_resolver(user):
    raise RuntimeError("resolver down")

from notifications import constants
from notifications.apps import NotificationsConfig
from notifications.models import Notification

# The notification framework must never depend on a business module -- the
# dependency direction is business-module -> notifications, never the reverse
# (architecture Rule 2 / Rule 19). Checked statically below.
BUSINESS_MODULES = {
    "orders",
    "payments",
    "deposits",
    # `approvals` was the per-module approval engine; it has been removed. The
    # generic Workflow Engine replaced it and the same direction rule applies.
    "workflow",
    "inventory",
    "invoices",
    "invoice",
    "einvoice",
    "ewaybill",
    "tracker",
    "sap_sync",
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

    def test_app_owns_the_notification_model(self):
        # Phase 3.3 introduces the framework's own Notification model — a NEW,
        # independent model (not the old Orders one). It must be owned by this
        # app.
        config = apps.get_app_config("notifications")
        model_names = {m.__name__ for m in config.get_models()}
        self.assertIn("Notification", model_names)


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


class NotificationModelTests(TestCase):
    """Phase 3.3: the new, independent Notification persistence model.

    Uses the SQLite test settings (migrations disabled → tables built from the
    models). Validates behaviour + persistence-level isolation; it does NOT
    exercise push providers or an HTTP API (those are later phases).
    """

    @classmethod
    def setUpTestData(cls):
        from users.models import Company

        User = get_user_model()
        cls.company_a = Company.objects.create(name="Company A")
        cls.company_b = Company.objects.create(name="Company B")
        cls.user_a = User.objects.create(username="alice", name="Alice", company=cls.company_a)
        cls.user_b = User.objects.create(username="bob", name="Bob", company=cls.company_b)

    def _notif(self, user, company, entity=None, **kw):
        kw.setdefault("event_type", constants.PAYMENT_APPROVED)
        kw.setdefault("message", "hello")
        n = Notification(user=user, company=company, **kw)
        if entity is not None:
            n.entity = entity
        n.save()
        return n

    # 1. creation + defaults
    def test_create_and_defaults(self):
        n = self._notif(self.user_a, self.company_a)
        self.assertIsNotNone(n.id)
        self.assertFalse(n.is_read)
        self.assertIsNotNone(n.created_at)
        self.assertEqual(n.event_type, constants.PAYMENT_APPROVED)

    # 2. user relationship
    def test_user_relationship(self):
        n = self._notif(self.user_a, self.company_a)
        self.assertEqual(n.user, self.user_a)
        self.assertIn(n, self.user_a.framework_notifications.all())

    # 3. company relationship
    def test_company_relationship(self):
        n = self._notif(self.user_a, self.company_a)
        self.assertEqual(n.company, self.company_a)
        self.assertIn(n, self.company_a.framework_notifications.all())

    # 4/11. generic entity resolution
    def test_generic_entity_resolution(self):
        n = self._notif(self.user_a, self.company_a, entity=self.company_b)
        n.refresh_from_db()
        self.assertEqual(n.content_type, ContentType.objects.get_for_model(type(self.company_b)))
        self.assertEqual(n.object_id, self.company_b.id)
        self.assertEqual(n.entity, self.company_b)

    def test_distinct_entity_types_have_distinct_content_types(self):
        from users.models import Company, MainGroup

        mg = MainGroup.objects.create(name="MG-1")
        n_company = self._notif(self.user_a, self.company_a, entity=self.company_a)
        n_group = self._notif(self.user_a, self.company_a, entity=mg)
        self.assertNotEqual(n_company.content_type, n_group.content_type)
        self.assertEqual(n_company.content_type, ContentType.objects.get_for_model(Company))
        self.assertEqual(n_group.content_type, ContentType.objects.get_for_model(MainGroup))

    # 5/6. Payment + Deposit entity TYPES are supported generically (no business FK)
    def test_payment_and_deposit_entities_supported(self):
        from django.apps import apps as django_apps

        receipt_ct = ContentType.objects.get_for_model(
            django_apps.get_model("payments", "PaymentReceipt")
        )
        deposit_ct = ContentType.objects.get_for_model(
            django_apps.get_model("payments", "BankDeposit")
        )
        n_pay = self._notif(
            self.user_a, self.company_a,
            event_type=constants.PAYMENT_APPROVED, content_type=receipt_ct, object_id=123,
        )
        n_dep = self._notif(
            self.user_a, self.company_a,
            event_type=constants.DEPOSIT_RECEIVED, content_type=deposit_ct, object_id=456,
        )
        self.assertEqual(n_pay.content_type.model, "paymentreceipt")
        self.assertEqual(n_dep.content_type.model, "bankdeposit")
        # No business FK columns exist on the model — proves it is entity-agnostic.
        field_names = {f.name for f in Notification._meta.get_fields()}
        for forbidden in ("order", "payment", "deposit", "invoice"):
            self.assertNotIn(forbidden, field_names)

    # 7. event type stored (framework accepts any valid event name; a business
    #    module owns its own names — here we use a framework-shipped one and a
    #    module-defined arbitrary one to prove the column is name-agnostic).
    def test_event_type_stored(self):
        n = self._notif(self.user_a, self.company_a, event_type=constants.DEPOSIT_RECEIVED)
        n.refresh_from_db()
        self.assertEqual(n.event_type, constants.DEPOSIT_RECEIVED)
        n2 = self._notif(self.user_a, self.company_a, event_type="INVOICE_ISSUED")
        n2.refresh_from_db()
        self.assertEqual(n2.event_type, "INVOICE_ISSUED")

    # 8. read/unread
    def test_read_unread(self):
        n = self._notif(self.user_a, self.company_a)
        self.assertFalse(n.is_read)
        n.is_read = True
        n.save(update_fields=["is_read"])
        n.refresh_from_db()
        self.assertTrue(n.is_read)

    # 9. user isolation
    def test_user_isolation(self):
        self._notif(self.user_a, self.company_a)
        self._notif(self.user_a, self.company_a)
        self._notif(self.user_b, self.company_b)
        a_qs = Notification.objects.filter(user=self.user_a)
        self.assertEqual(a_qs.count(), 2)
        self.assertNotIn(
            self.user_b.id, set(a_qs.values_list("user_id", flat=True))
        )

    # 10. company isolation
    def test_company_isolation(self):
        self._notif(self.user_a, self.company_a)
        self._notif(self.user_b, self.company_b)
        self.assertEqual(Notification.objects.filter(company=self.company_a).count(), 1)
        self.assertEqual(
            set(Notification.objects.filter(company=self.company_b).values_list("company_id", flat=True)),
            {self.company_b.id},
        )

    # 12. invalid / dangling entity resolves to None (never raises)
    def test_dangling_entity_resolves_none(self):
        ct = ContentType.objects.get_for_model(type(self.company_a))
        n = self._notif(self.user_a, self.company_a, content_type=ct, object_id=99_999_999)
        self.assertIsNone(n.entity)

    def test_entityless_notification_is_valid(self):
        n = self._notif(self.user_a, self.company_a)
        self.assertIsNone(n.content_type)
        self.assertIsNone(n.object_id)
        self.assertIsNone(n.entity)

    # 13. payload-ready data present
    def test_payload_ready_fields_present(self):
        n = self._notif(self.user_a, self.company_a, title="Payment approved")
        for f in [
            "id", "user_id", "company_id", "event_type", "title", "message",
            "content_type_id", "object_id", "is_read", "created_at",
        ]:
            self.assertTrue(hasattr(n, f), f"missing payload field: {f}")

    def test_uses_new_public_table_not_old_orders_table(self):
        # New framework table, pinned to the PUBLIC schema, never the old Orders
        # `notifications` table. Postgres sees 'public"."notifications_notification';
        # the SQLite test settings flatten it to 'public_notifications_notification'
        # via the project's _flatten_schema_qualified_tables shim.
        db_table = Notification._meta.db_table
        self.assertNotEqual(db_table, "notifications")
        self.assertIn("notifications_notification", db_table)
        self.assertTrue(db_table.startswith("public"))


class NotificationDispatcherTests(TestCase):
    """Phase 3.4: the reusable engine — notify(), dispatch, providers.

    External Expo/Web Push network calls are mocked; no real push is sent.
    """

    @classmethod
    def setUpTestData(cls):
        from users.models import Company

        User = get_user_model()
        cls.company_a = Company.objects.create(name="Disp Co A")
        cls.company_b = Company.objects.create(name="Disp Co B")
        cls.u1 = User.objects.create(username="d_alice", name="Alice", company=cls.company_a)
        cls.u2 = User.objects.create(username="d_bob", name="Bob", company=cls.company_a)
        cls.u3 = User.objects.create(username="d_carol", name="Carol", company=cls.company_b)

    @staticmethod
    def _notify(**kw):
        from notifications.services import notify

        kw.setdefault("event_type", constants.PAYMENT_APPROVED)
        kw.setdefault("title", "Title")
        kw.setdefault("message", "Message")
        return notify(**kw)

    def _spy_provider(self, channel, result=None, side_effect=None):
        from notifications.providers.base import ProviderResult

        p = mock.Mock()
        p.channel = channel
        if side_effect is not None:
            p.send.side_effect = side_effect
        else:
            p.send.return_value = result or ProviderResult(channel, delivered=1)
        return p

    # 1
    def test_notify_creates_notification(self):
        created = self._notify(recipients=[self.u1])
        self.assertEqual(len(created), 1)
        n = created[0]
        self.assertEqual(n.user, self.u1)
        self.assertEqual(n.event_type, constants.PAYMENT_APPROVED)
        self.assertFalse(n.is_read)

    # 2 + 16
    def test_multiple_recipients_fan_out(self):
        created = self._notify(recipients=[self.u1, self.u2])
        self.assertEqual({n.user_id for n in created}, {self.u1.id, self.u2.id})

    # 3
    def test_recipient_validation(self):
        with self.assertRaises(TypeError):
            self._notify(recipients=["not-a-user"])

    # 4 + 5
    def test_entity_gfk_creation_arbitrary_type(self):
        from users.models import MainGroup

        mg = MainGroup.objects.create(name="Disp MG")
        n = self._notify(recipients=[self.u1], entity=mg)[0]
        n.refresh_from_db()
        self.assertEqual(n.content_type, ContentType.objects.get_for_model(MainGroup))
        self.assertEqual(n.object_id, mg.id)
        self.assertEqual(n.entity, mg)

    # 6
    def test_event_type_validation(self):
        with self.assertRaises(ValueError):
            self._notify(event_type="not valid lowercase", recipients=[self.u1])

    # 7
    def test_title_message_persistence(self):
        n = self._notify(recipients=[self.u1], title="Hello", message="World")[0]
        n.refresh_from_db()
        self.assertEqual((n.title, n.message), ("Hello", "World"))

    # 8
    def test_company_from_recipient(self):
        n = self._notify(recipients=[self.u1])[0]
        self.assertEqual(n.company_id, self.company_a.id)

    # 9
    def test_company_isolation_skips_mismatched(self):
        created = self._notify(recipients=[self.u1, self.u3], company=self.company_a)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].user_id, self.u1.id)
        self.assertEqual(created[0].company_id, self.company_a.id)

    # 10
    def test_user_isolation(self):
        self._notify(recipients=[self.u1])
        self._notify(recipients=[self.u3])
        self.assertEqual(Notification.objects.filter(user=self.u1).count(), 1)
        self.assertNotIn(
            self.u3.id,
            set(Notification.objects.filter(user=self.u1).values_list("user_id", flat=True)),
        )

    # 11 + 12 + 13
    def test_canonical_payload(self):
        from notifications.services.payloads import build_payload
        from users.models import Company

        n = self._notify(recipients=[self.u1], entity=self.company_a, title="Ti", message="Me")[0]
        p = build_payload(n)
        self.assertEqual(p["notification_id"], n.id)
        self.assertEqual(p["event_type"], constants.PAYMENT_APPROVED)
        self.assertEqual((p["title"], p["message"]), ("Ti", "Me"))
        self.assertEqual(p["entity_type"], ContentType.objects.get_for_model(Company).model)
        self.assertEqual(p["entity_id"], self.company_a.id)
        self.assertEqual(p["company_id"], n.company_id)
        self.assertNotIn("order_id", p)  # generic, never order-specific

    # 14 + 16
    def test_provider_invoked_per_recipient(self):
        spy = self._spy_provider("mobile_push")
        with mock.patch("notifications.services.dispatcher.default_providers", return_value=[spy]):
            with self.captureOnCommitCallbacks(execute=True):
                self._notify(recipients=[self.u1, self.u2])
        self.assertEqual(spy.send.call_count, 2)

    # 15
    def test_provider_failure_is_isolated(self):
        from notifications.providers.base import ProviderResult

        p_fail = self._spy_provider("mobile_push", side_effect=RuntimeError("boom"))
        p_ok = self._spy_provider("web_push", result=ProviderResult("web_push", delivered=1))
        with mock.patch(
            "notifications.services.dispatcher.default_providers",
            return_value=[p_fail, p_ok],
        ):
            with self.captureOnCommitCallbacks(execute=True):
                # Must not raise despite p_fail exploding.
                self._notify(recipients=[self.u1])
        p_fail.send.assert_called_once()
        p_ok.send.assert_called_once()  # other provider still delivered

    # 17
    def test_delivery_deferred_until_commit(self):
        spy = self._spy_provider("mobile_push")
        with mock.patch("notifications.services.dispatcher.default_providers", return_value=[spy]):
            with self.captureOnCommitCallbacks(execute=False) as callbacks:
                self._notify(recipients=[self.u1])
                self.assertFalse(spy.send.called)  # NOT delivered before commit
            self.assertEqual(len(callbacks), 1)  # exactly one delivery callback
            for cb in callbacks:
                cb()  # simulate commit
            self.assertTrue(spy.send.called)  # delivered on commit

    # 18
    def test_rollback_creates_no_records_and_no_delivery(self):
        from django.db import transaction

        spy = self._spy_provider("mobile_push")
        before = Notification.objects.count()
        with mock.patch("notifications.services.dispatcher.default_providers", return_value=[spy]):
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    self._notify(recipients=[self.u1])
                    raise RuntimeError("rollback")
        self.assertEqual(Notification.objects.count(), before)  # records rolled back
        self.assertFalse(spy.send.called)  # no delivery for a rolled-back event

    # real providers path is safe with no token source (no network, no error)
    def test_real_providers_run_safely_without_tokens(self):
        with self.captureOnCommitCallbacks(execute=True):
            created = self._notify(recipients=[self.u1])
        self.assertEqual(len(created), 1)  # delivery skipped cleanly, no exception


class NotificationResolverSeamTests(TestCase):
    """Phase 3.6: providers resolve registrations via the settings-configured
    resolver seam — no business module imported by the framework."""

    @classmethod
    def setUpTestData(cls):
        from users.models import Company

        User = get_user_model()
        cls.company = Company.objects.create(name="Seam Co")
        cls.user = User.objects.create(username="seam_u", name="Seam", company=cls.company)

    @override_settings(NOTIFICATION_MOBILE_TOKEN_RESOLVER="notifications.tests._fake_mobile_resolver")
    def test_mobile_provider_uses_configured_resolver(self):
        from notifications.providers.mobile import MobileProvider

        self.assertEqual(MobileProvider().get_tokens(self.user), ["ExponentPushToken[SEAM]"])

    @override_settings(NOTIFICATION_WEB_SUBSCRIPTION_RESOLVER="notifications.tests._fake_web_resolver")
    def test_web_provider_uses_configured_resolver(self):
        from notifications.providers.web import WebProvider

        subs = WebProvider().get_subscriptions(self.user)
        self.assertEqual(subs[0]["endpoint"], "https://push.example/seam")

    @override_settings(NOTIFICATION_MOBILE_TOKEN_RESOLVER="")
    def test_unset_resolver_is_noop(self):
        from notifications.providers.mobile import MobileProvider

        self.assertEqual(MobileProvider().get_tokens(self.user), [])

    @override_settings(NOTIFICATION_MOBILE_TOKEN_RESOLVER="notifications.tests._boom_resolver")
    def test_resolver_exception_is_isolated(self):
        from notifications.providers.mobile import MobileProvider

        # Must not raise; returns [] and logs.
        self.assertEqual(MobileProvider().get_tokens(self.user), [])

    @override_settings(NOTIFICATION_MOBILE_TOKEN_RESOLVER="notifications.tests._fake_mobile_resolver")
    def test_end_to_end_notify_reaches_provider_with_resolved_token(self):
        from notifications.providers import mobile
        from notifications.services import notify

        with mock.patch.object(mobile, "requests") as m_requests:
            m_requests.post.return_value.raise_for_status.return_value = None
            with self.captureOnCommitCallbacks(execute=True):
                notify(
                    event_type=constants.PAYMENT_APPROVED,
                    title="T", message="M", recipients=[self.user],
                )
        self.assertTrue(m_requests.post.called)
        message = m_requests.post.call_args.kwargs["json"][0]
        self.assertEqual(message["to"], "ExponentPushToken[SEAM]")


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
            # Test files legitimately import business models to build fixtures —
            # that is a testing concern, not a runtime dependency. The rule that
            # matters is that the FRAMEWORK's production modules (models,
            # services, providers, serializers, views, urls) import no business
            # module; those are still scanned.
            if "__pycache__" in py.parts or py.name.startswith("test"):
                continue
            bad = self._top_level_imports(py) & BUSINESS_MODULES
            if bad:
                offenders[str(py.relative_to(pkg_dir))] = sorted(bad)
        self.assertEqual(
            offenders,
            {},
            f"notifications must not import business modules: {offenders}",
        )
