from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    """The reusable notification framework.

    Currently an empty skeleton (Phase 3.1). It exists ahead of any behaviour
    so that the INSTALLED_APPS change lands on its own and stays bisectable:
    if a later phase breaks startup, `git bisect` points at the phase that
    added the code, not at the phase that registered the app.

    Nothing imports this app yet and this app imports nothing. Orders keeps
    serving every notification exactly as before.
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
