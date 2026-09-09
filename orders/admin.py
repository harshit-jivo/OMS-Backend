from django.contrib import admin

from .models import (
    Notification,
    OrderFlowConfig,
    PushToken,
    StaffProductPrice,
    WebPushSubscription,
)

# @admin.register(OrderLog)
admin.site.register(StaffProductPrice)
admin.site.register(OrderFlowConfig)


# ---------------------------------------------------------------------------
# Notification delivery — read-only operational views
# ---------------------------------------------------------------------------
# These three models had no admin at all, so the only way to answer "was this
# user actually notified?" or "does this device still have a live token?" was a
# database client. They are registered here as an operational surface for the
# notification framework migration, where being able to see rows during a
# cutover is the difference between a confident deploy and a guess.
#
# Deliberately READ-ONLY. They are delivery records, not configuration:
# editing a notification's text after the fact would make the history lie, and
# a hand-edited push token would simply fail at the push service. Tokens and
# subscriptions are retired by the nightly prune commands, which record why.


class _ReadOnlyAdmin(admin.ModelAdmin):
    """No add, no edit, no delete — inspection only."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Notification)
class NotificationAdmin(_ReadOnlyAdmin):
    list_display = ('id', 'user', 'order', 'short_message', 'is_read', 'created_at')
    list_filter = ('is_read', 'created_at')
    search_fields = ('message', 'user__username', 'user__name', 'order__order_number')
    date_hierarchy = 'created_at'
    # The list joins both FKs on every row; without this it is one query per
    # row per relation on a 100-row page.
    list_select_related = ('user', 'order')

    @admin.display(description='Message')
    def short_message(self, obj):
        text = obj.message or ''
        return text if len(text) <= 80 else f'{text[:77]}…'


@admin.register(PushToken)
class PushTokenAdmin(_ReadOnlyAdmin):
    list_display = ('id', 'user', 'platform', 'is_active', 'updated_at')
    list_filter = ('is_active', 'platform')
    search_fields = ('token', 'user__username', 'user__name')
    list_select_related = ('user',)


@admin.register(WebPushSubscription)
class WebPushSubscriptionAdmin(_ReadOnlyAdmin):
    list_display = ('id', 'user', 'short_endpoint', 'is_active', 'updated_at')
    list_filter = ('is_active',)
    search_fields = ('endpoint', 'user__username', 'user__name')
    list_select_related = ('user',)

    @admin.display(description='Endpoint')
    def short_endpoint(self, obj):
        # Endpoints are long push-service URLs; the host plus a short tail is
        # enough to tell two browsers apart.
        endpoint = obj.endpoint or ''
        return endpoint if len(endpoint) <= 60 else f'{endpoint[:57]}…'
