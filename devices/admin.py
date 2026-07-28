"""Django admin for device tracking.

UserDevice is presented read-only — rows are written by the API from client
telemetry, and hand-editing them would corrupt analytics. There is nothing here
for an admin to curate: each client reports its own version and build.
"""
from django.contrib import admin

from .models import UserDevice, VersionPolicy
from .version_policy import clear_policy_cache


@admin.register(UserDevice)
class UserDeviceAdmin(admin.ModelAdmin):
    list_display = [
        "user",
        "platform",
        "app_type",
        "app_version",
        "build_number",
        "device_name",
        "browser_name",
        "os_name",
        "os_version",
        "is_active",
        "last_active",
    ]
    list_filter = ["platform", "app_type", "is_active", "browser_name", "last_active"]
    search_fields = [
        "user__username",
        "user__name",
        "device_id",
        "device_name",
        "manufacturer",
        "device_model",
        "app_version",
    ]
    date_hierarchy = "last_active"
    ordering = ["-last_active"]
    list_select_related = ["user"]
    # Telemetry is API-owned; the admin is a read-only window onto it.
    readonly_fields = [f.name for f in UserDevice._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # Allow deletion so admins can prune obviously-bogus rows; the scheduled
        # retention job (a later phase) handles routine cleanup.
        return True


@admin.register(VersionPolicy)
class VersionPolicyAdmin(admin.ModelAdmin):
    """The mobile version policy is admin-curated (unlike UserDevice). Editable
    here as well as via the web admin API; both invalidate the middleware cache
    so a change applies immediately."""

    list_display = [
        "platform",
        "required_version",
        "required_build",
        "is_active",
        "updated_at",
    ]
    list_filter = ["platform", "is_active"]
    readonly_fields = ["created_at", "updated_at"]

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        clear_policy_cache()

    def delete_model(self, request, obj):
        super().delete_model(request, obj)
        clear_policy_cache()
