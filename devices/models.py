"""Device & App Version Management — persistence layer (Phase 2).

Two tables, both new; no existing table is modified.

- :class:`UserDevice` — the current state of one installed app on one device,
  for one user. One row per ``(user, device_id)``. This is telemetry and
  compatibility data, never an authorization source (see the security notes in
  the Phase 1 design). Existing identity data (name, role, company, ...) lives
  on ``users.User`` and is joined via the FK, never copied here.

- :class:`AppRelease` — the version *policy* for a platform + app_type: what the
  latest build is, what the minimum supported build is, and the release notes.
  Admin-editable so policy can change without a code deploy.

Design decisions (indexes, uniqueness, the two-axis platform/app_type model,
why ``build_number`` is the comparison key and version strings never are) are
documented inline and in the Phase 1 foundation design.

Force-update enforcement is intentionally NOT implemented in this phase — the
policy fields exist on :class:`AppRelease` so later phases can read them, but no
code here makes an update-required decision.
"""
from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models


# ---------------------------------------------------------------------------
# Shared vocabulary — two independent axes, defined once and used by BOTH
# models. Kept as CharField + choices (not a Postgres ENUM type, not a lookup
# table) so a future value like PARTNER_WEB is a state-only migration with no
# DDL against these tables. See the Phase 1 "future compatibility" section.
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

# Strict semantic-version validator — applied only to AppRelease.version, the
# rows we author ourselves. Device-reported versions are stored loosely (a
# hostile/buggy client must never break the session with a bad string).
SEMVER_VALIDATOR = RegexValidator(
    regex=r"^\d+\.\d+\.\d+$",
    message="Version must be in Major.Minor.Patch form, e.g. 1.2.0.",
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
    # Hot column: written on login/refresh and via a throttled heartbeat. Indexed
    # for the outdated-device / last-seen queries.
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


class AppRelease(models.Model):
    """Version policy for one platform + app_type.

    Authored by admins; the source of truth for "what should be running". The
    ``min_supported_*`` and ``is_force_update`` fields exist here so a later
    phase can build the update gate — this phase stores them but enforces
    nothing.
    """

    platform = models.CharField(max_length=20, choices=PLATFORM_CHOICES)
    app_type = models.CharField(max_length=20, choices=APP_TYPE_CHOICES)

    # Strictly validated — we write these rows, so we can demand rigor.
    version = models.CharField(max_length=20, validators=[SEMVER_VALIDATOR])
    build_number = models.PositiveIntegerField()

    release_notes = models.TextField(blank=True, default="")

    # Denormalized "this is the current release" pointer. Kept single per
    # (platform, app_type) by the partial unique constraint below.
    is_latest = models.BooleanField(default=False)

    # Force-update policy fields. STORED ONLY in this phase — no code here reads
    # them to make an enforcement decision. Enforcement is a later phase.
    is_force_update = models.BooleanField(default=False)
    min_supported_version = models.CharField(
        max_length=20, blank=True, default="", validators=[SEMVER_VALIDATOR]
    )
    # The integer the gate will actually compare against (strings never are).
    min_supported_build = models.PositiveIntegerField(null=True, blank=True)

    # Where a future update prompt sends the user. Per-platform, so it lives here.
    store_url = models.URLField(blank=True, default="")

    # Explicit (not auto_now_add): a release row is staged before it goes live.
    released_at = models.DateTimeField()
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "devices_app_release"
        verbose_name = "App Release"
        ordering = ["-released_at"]
        constraints = [
            # One release row per build per product.
            models.UniqueConstraint(
                fields=["platform", "app_type", "build_number"],
                name="apprelease_build_uq",
            ),
            # At most ONE latest release per product — enforced in the database
            # so a "latest" pointer can never be ambiguous (which would make the
            # version endpoint nondeterministic). Publishing code must clear the
            # old flag in the same transaction. Partial unique index (PostgreSQL).
            models.UniqueConstraint(
                fields=["platform", "app_type"],
                condition=models.Q(is_latest=True),
                name="apprelease_one_latest_uq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["platform", "app_type", "is_latest"],
                name="apprelease_lookup_idx",
            ),
        ]

    def __str__(self):
        return f"{self.platform}/{self.app_type} v{self.version} (build {self.build_number})"
