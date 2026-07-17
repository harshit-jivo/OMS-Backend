"""Device & App Version Management — persistence layer.

One table; no existing table is modified.

- :class:`UserDevice` — the current state of one installed app on one device,
  for one user. One row per ``(user, device_id)``. This is telemetry and
  compatibility data, never an authorization source (see the security notes in
  the Phase 1 design). Existing identity data (name, role, company, ...) lives
  on ``users.User`` and is joined via the FK, never copied here.

The deployed app is the sole source of truth for its own version and build:
each client reports what it is actually running, and this table records it.
There is deliberately no server-side release/policy table — nothing here
decides what *should* be running, so no admin action is needed after a deploy.

Design decisions (indexes, the two-axis platform/app_type model, why
``build_number`` is the comparison key and version strings never are) are
documented inline and in the Phase 1 foundation design.
"""
from django.conf import settings
from django.db import models


# ---------------------------------------------------------------------------
# Shared vocabulary — two independent axes. Kept as CharField + choices (not a
# Postgres ENUM type, not a lookup table) so a future value like PARTNER_WEB is
# a state-only migration with no DDL against this table. See the Phase 1
# "future compatibility" section.
# ---------------------------------------------------------------------------

# platform = the runtime the code executes on.
PLATFORM_ANDROID = "ANDROID"
PLATFORM_IOS = "IOS"
PLATFORM_WEB = "WEB"
PLATFORM_DESKTOP = "DESKTOP"
PLATFORM_CHOICES = (
    (PLATFORM_ANDROID, "Android"),
    (PLATFORM_IOS, "iOS"),
    (PLATFORM_WEB, "Web"),
    (PLATFORM_DESKTOP, "Desktop"),
)

# app_type = which product it is. A browser can be the sales web app, the admin
# portal, or a partner portal — same platform, different release train.
APP_TYPE_MOBILE = "MOBILE"
APP_TYPE_TABLET = "TABLET"
APP_TYPE_WEB = "WEB"
APP_TYPE_ADMIN_WEB = "ADMIN_WEB"
APP_TYPE_PARTNER_WEB = "PARTNER_WEB"
APP_TYPE_DESKTOP = "DESKTOP"
APP_TYPE_CHOICES = (
    (APP_TYPE_MOBILE, "Mobile"),
    (APP_TYPE_TABLET, "Tablet"),
    (APP_TYPE_WEB, "Web"),
    (APP_TYPE_ADMIN_WEB, "Admin Web"),
    (APP_TYPE_PARTNER_WEB, "Partner Web"),
    (APP_TYPE_DESKTOP, "Desktop"),
)


class UserDevice(models.Model):
    """One installed app on one device, for one user.

    Uniqueness is ``(user, device_id)`` — per user, never global — so a shared
    tablet used by two staff is two rows, and a forged ``device_id`` can only
    ever touch the forger's own rows.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="devices",
    )

    # Client-generated opaque UUIDv4, persisted on the device and NOT cleared on
    # logout. CharField (not UUIDField) so a malformed value fails cleanly in
    # serializer validation rather than raising at the database layer.
    device_id = models.CharField(max_length=64)

    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES)
    app_type = models.CharField(max_length=20, choices=APP_TYPE_CHOICES)

    # Reported version string (display) + build number (the comparison key).
    app_version = models.CharField(max_length=20)
    build_number = models.PositiveIntegerField()

    # Device facts. Blank on platforms where they don't apply.
    device_name = models.CharField(max_length=100, blank=True, default="")
    manufacturer = models.CharField(max_length=50, blank=True, default="")
    # Named device_model (not `model`) to avoid colliding with Django's own
    # "model" vocabulary at call sites.
    device_model = models.CharField(max_length=100, blank=True, default="")

    # OS split into name + version so "below Android 13" is a query, not a parse.
    os_name = models.CharField(max_length=20, blank=True, default="")
    os_version = models.CharField(max_length=20, blank=True, default="")

    # Web only; server-derived from the User-Agent header, never client-reported.
    browser_name = models.CharField(max_length=30, blank=True, default="")
    browser_version = models.CharField(max_length=30, blank=True, default="")

    language = models.CharField(max_length=10, blank=True, default="")  # BCP-47
    timezone = models.CharField(max_length=64, blank=True, default="")  # IANA name

    # first_login is set once and never updated; distinct from created_at so the
    # two can legitimately diverge if a row is ever created outside a login.
    first_login = models.DateTimeField()
    last_login = models.DateTimeField()
    # Hot column: written on login/refresh and via a throttled heartbeat.
    # Indexed for the last-seen / activity-status queries.
    last_active = models.DateTimeField()

    is_active = models.BooleanField(default=True)  # soft-delete for aged-out devices

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "devices_user_device"
        verbose_name = "User Device"
        ordering = ["-last_active"]
        constraints = [
            # One row per install per user. This is the load-bearing invariant.
            models.UniqueConstraint(
                fields=["user", "device_id"], name="device_user_deviceid_uq"
            ),
        ]
        indexes = [
            # "The caller's active devices" — profile screens, register lookups.
            models.Index(fields=["user", "is_active"], name="device_user_active_idx"),
            # Version-distribution analytics, always grouped by platform+app_type.
            models.Index(
                fields=["platform", "app_type", "build_number"],
                name="device_plat_build_idx",
            ),
            # Retention / last-seen sweeps.
            models.Index(fields=["last_active"], name="device_last_active_idx"),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.platform}/{self.app_type} v{self.app_version} ({self.device_id[:8]})"
