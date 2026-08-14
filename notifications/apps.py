from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    """The reusable notification framework.

    Phase 3.2 adds the framework FOUNDATION only: the event-name contract
    (`constants.py`) and the empty event registry (`registry.py`). There are
    still no models, no migrations, and no delivery behaviour — Orders keeps
    serving every notification exactly as before.

    This app imports only its OWN submodules; it imports no business module
    (orders/payments/approvals/inventory/invoices/users), preserving the
    one-way dependency direction business-module → notifications.
    """

    # ⚠️ AutoField, NOT BigAutoField — deliberate, and the opposite of every
    # other recently added app here (payments, approvals, core, devices … all
    # use BigAutoField).
    #
    # The three notification models will move into this app in a later phase
    # via SeparateDatabaseAndState, which is a state-only change that must emit
    # no DDL. `orders` sets no default_auto_field, so it inherits AutoField
    # from settings.DEFAULT_AUTO_FIELD, and the live `notifications`,
    # `push_tokens` and `web_push_subscriptions` tables all have plain
    # `integer` primary keys.
    #
    # `PushToken` pins `id = models.AutoField(primary_key=True)` explicitly,
    # but `Notification` and `WebPushSubscription` inherit theirs. Declaring
    # BigAutoField here would make makemigrations emit an AlterField OUTSIDE
    # the SeparateDatabaseAndState wrapper — a real ALTER TABLE ... TYPE bigint
    # rewriting live tables and every FK pointing at them. Invisible on today's
    # row counts, expensive later.
    default_auto_field = 'django.db.models.AutoField'

    name = 'notifications'
    verbose_name = 'Notification Framework'

    def ready(self):
        # Load the event registry at startup so its registration point exists
        # for business modules to register into (from their OWN AppConfig.ready())
        # in a later phase. This mirrors how the approvals engine exposes its
        # `_HOOKS` registry. It performs no database query, no network call and
        # no business logic, and imports only this app's own submodule — never a
        # business module, so no circular import is possible.
        from . import registry  # noqa: F401
