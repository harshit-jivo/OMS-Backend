from django.apps import AppConfig


class OrdersConfig(AppConfig):
    name = 'orders'

    def ready(self):
        # Register the VAPID key-pair validation so a mismatched pair fails at
        # startup instead of silently breaking Web Push for every browser.
        from . import checks  # noqa: F401
