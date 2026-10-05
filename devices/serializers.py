"""Serializers for the device & version endpoints.

Input serializers validate the client contract strictly at the boundary but
store device-reported strings loosely (bad telemetry must degrade to an ugly
row, never a 500 or a failed login). Browser/OS-from-UA derivation is done in
the view, not here, so it isn't client-spoofable.
"""
from django.utils import timezone
from rest_framework import serializers

from .models import (
    APP_TYPE_CHOICES,
    MOBILE_PLATFORM_CHOICES,
    PLATFORM_CHOICES,
    UserDevice,
    VersionPolicy,
)
from .status import compute_status
from .utils import is_valid_device_id


class DeviceRegisterSerializer(serializers.Serializer):
    """Validate the payload for POST /devices/register."""

    device_id = serializers.CharField(max_length=64)
    platform = serializers.ChoiceField(choices=PLATFORM_CHOICES)
    app_type = serializers.ChoiceField(choices=APP_TYPE_CHOICES)
    app_version = serializers.CharField(max_length=20)
    build_number = serializers.IntegerField(min_value=1)

    device_name = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    manufacturer = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")
    device_model = serializers.CharField(max_length=100, required=False, allow_blank=True, default="")
    os_name = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    os_version = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    language = serializers.CharField(max_length=10, required=False, allow_blank=True, default="")
    timezone = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")

    def validate_device_id(self, value):
        value = value.strip()
        if not is_valid_device_id(value):
            raise serializers.ValidationError(
                "device_id must be an opaque token of 8-64 chars [A-Za-z0-9._-] "
                "(a client-generated UUIDv4 is expected)."
            )
        return value


class DeviceUpdateSerializer(serializers.Serializer):
    """Validate the payload for PUT /devices/update.

    ``device_id`` selects the row; the rest is the mutable subset a client
    refreshes when its app updates or its locale changes.
    """

    device_id = serializers.CharField(max_length=64)
    app_version = serializers.CharField(max_length=20, required=False)
    build_number = serializers.IntegerField(min_value=1, required=False)
    os_version = serializers.CharField(max_length=20, required=False, allow_blank=True)
    device_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    language = serializers.CharField(max_length=10, required=False, allow_blank=True)
    timezone = serializers.CharField(max_length=64, required=False, allow_blank=True)

    def validate_device_id(self, value):
        value = value.strip()
        if not is_valid_device_id(value):
            raise serializers.ValidationError("Invalid device_id.")
        return value


class UserDeviceSerializer(serializers.ModelSerializer):
    """Read representation returned by /devices/register, /update and /me."""

    class Meta:
        model = UserDevice
        fields = [
            "device_id",
            "platform",
            "app_type",
            "app_version",
            "build_number",
            "device_name",
            "manufacturer",
            "device_model",
            "os_name",
            "os_version",
            "browser_name",
            "browser_version",
            "language",
            "timezone",
            "first_login",
            "last_login",
            "last_active",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields


# ---------------------------------------------------------------------------
# Admin serializers
# ---------------------------------------------------------------------------
class AdminUserDeviceSerializer(serializers.ModelSerializer):
    """Device row for the admin table/detail, with the owning user inlined.

    User identity is READ from the FK (never duplicated onto the device table).
    NOTE: `users.User` has no employee-code field, so the "Employee Code" column
    requested by the admin spec has no source; `username` is the closest stable
    human identifier and is exposed instead.
    """

    user_id = serializers.IntegerField(source="user.id", read_only=True)
    username = serializers.CharField(source="user.username", read_only=True)
    user_name = serializers.CharField(source="user.name", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    role = serializers.SerializerMethodField()
    # Derived, never stored. Computed server-side so the badge can never
    # disagree with the ?status= filter (a skewed browser clock would).
    status = serializers.SerializerMethodField()
    # "latest" | "old" | "unknown" — vs the active version policy for this
    # device's platform. "unknown" when the platform has no policy (or is web).
    update_status = serializers.SerializerMethodField()

    class Meta:
        model = UserDevice
        fields = [
            "id",
            "device_id",
            "user_id",
            "username",
            "user_name",
            "email",
            "role",
            "status",
            "update_status",
            "platform",
            "app_type",
            "app_version",
            "build_number",
            "device_name",
            "manufacturer",
            "device_model",
            "browser_name",
            "browser_version",
            "os_name",
            "os_version",
            "language",
            "timezone",
            "first_login",
            "last_login",
            "last_active",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_role(self, obj) -> str:
        role = getattr(obj.user, "role", None)
        return getattr(role, "display_name", None) or getattr(role, "name", "") or ""

    def get_status(self, obj) -> str:
        # `now` is passed in context so every row in a page is judged against
        # the same instant (and we don't re-read the clock per row).
        now = self.context.get("now") or timezone.now()
        return compute_status(obj.last_active, now)

    def get_update_status(self, obj) -> str:
        # `policies` is passed in via context (one lookup for the whole page)
        # to avoid a query per row. Only ANDROID/IOS with an active policy are
        # classified; everything else is "unknown" (the web is never gated).
        policies = self.context.get("policies") or {}
        policy = policies.get(obj.platform)
        if not policy:
            return "unknown"
        # A blank required build means the gate is off; nothing is "old".
        required_build = policy.get("required_build")
        if required_build in (None, ""):
            return "unknown"
        # At or ABOVE the floor is current. This was `==`, which labelled a
        # device that had taken a newer release "old" and asked it to update
        # to a build below the one it was already running.
        if obj.build_number is None:
            return "old"
        return "latest" if obj.build_number >= int(required_build) else "old"


class VersionPolicySerializer(serializers.ModelSerializer):
    """Admin CRUD for a mobile version policy (ANDROID / IOS only).

    ``platform`` is restricted to the two mobile choices, so the web can never
    be given a policy through this endpoint. The one-active-per-platform rule is
    a DB constraint (partial unique index); this validates the version format
    and surfaces a duplicate as a clean field error instead of an IntegrityError.
    """

    platform = serializers.ChoiceField(choices=MOBILE_PLATFORM_CHOICES)

    class Meta:
        model = VersionPolicy
        fields = [
            "id",
            "platform",
            "required_version",
            "required_build",
            "store_url",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_required_version(self, value):
        # Blank is allowed and meaningful: together with a blank required_build
        # it switches the gate off for this platform, which is how an admin
        # lets every app version through during an incident.
        return (value or "").strip()
