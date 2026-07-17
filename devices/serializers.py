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
    PLATFORM_CHOICES,
    AppRelease,
    UserDevice,
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


class AppVersionQuerySerializer(serializers.Serializer):
    """Validate query params for GET /app/version."""

    platform = serializers.ChoiceField(choices=PLATFORM_CHOICES)
    app_type = serializers.ChoiceField(choices=APP_TYPE_CHOICES)
    # Optional: when supplied, the response can flag whether a newer build exists.
    build_number = serializers.IntegerField(min_value=0, required=False)


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


class AppReleaseSerializer(serializers.ModelSerializer):
    """Create/update/read an AppRelease (the version-policy table).

    Enforces the safety rules in serializer-space so the admin UI gets clean
    field errors instead of a database IntegrityError:
      1. version / min_supported_version must be Major.Minor.Patch (model
         validators, inherited automatically).
      2. build_number must be unique per (platform, app_type).
      3. only one release may be `is_latest` per (platform, app_type) — the
         swap itself happens atomically in the view/service, this only reports
         conflicts that validation can catch early.
    """

    class Meta:
        model = AppRelease
        fields = [
            "id",
            "platform",
            "app_type",
            "version",
            "build_number",
            "release_notes",
            "is_latest",
            "is_force_update",
            "min_supported_version",
            "min_supported_build",
            "store_url",
            "released_at",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
        # DRF auto-generates UniqueTogetherValidators from the model's
        # UniqueConstraints — including the CONDITIONAL one
        # (`(platform, app_type) WHERE is_latest`). That validator evaluates its
        # condition via `attrs['is_latest']` and raises KeyError whenever the
        # field isn't supplied (any create/patch that omits it). Both rules are
        # enforced deliberately instead:
        #   • duplicate build_number -> validate() below, as a field error;
        #   • single latest per product -> atomic demote in the admin view,
        #     backstopped by the database's partial unique index.
        # Field-level validators (the semver regex) are unaffected by this.
        validators = []

    def validate(self, attrs):
        # Resolve effective values for partial updates.
        instance = self.instance
        platform = attrs.get("platform") or getattr(instance, "platform", None)
        app_type = attrs.get("app_type") or getattr(instance, "app_type", None)
        build_number = attrs.get("build_number", getattr(instance, "build_number", None))

        # Rule 2 — no duplicate build number for the same product. Mirrors the
        # DB constraint `apprelease_build_uq`; checked here for a clean message.
        if platform and app_type and build_number is not None:
            clash = AppRelease.objects.filter(
                platform=platform, app_type=app_type, build_number=build_number
            )
            if instance is not None:
                clash = clash.exclude(pk=instance.pk)
            if clash.exists():
                raise serializers.ValidationError(
                    {
                        "build_number": (
                            f"Build {build_number} already exists for "
                            f"{platform}/{app_type}. Build numbers must be unique "
                            "per platform and app type."
                        )
                    }
                )

        # A minimum supported build can never exceed the build it belongs to —
        # that would mark the release itself as unsupported.
        min_supported_build = attrs.get(
            "min_supported_build", getattr(instance, "min_supported_build", None)
        )
        if (
            min_supported_build is not None
            and build_number is not None
            and min_supported_build > build_number
        ):
            raise serializers.ValidationError(
                {
                    "min_supported_build": (
                        "Minimum supported build cannot be greater than this "
                        "release's build number."
                    )
                }
            )
        return attrs
