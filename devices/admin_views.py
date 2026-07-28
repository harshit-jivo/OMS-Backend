"""Admin API for Device Management.

Endpoints (all admin-only):

    GET    /api/admin/devices/            paginated + filtered + searchable list
    GET    /api/admin/devices/analytics/  cards + chart series
    GET    /api/admin/devices/<pk>/       one device + its user

Reads the existing `devices_user_device` table — no new tables. Follows the
project's house style: plain `APIView`, manual query-param handling,
`{success, message, data}` envelope, aggregation via `.values().annotate()`
(the same shape as `orders.views.DashboardChartsView`).

Scope: read/report only. Nothing here decides what a client *should* be
running — there is no release policy to compare against. The tables simply
report the version and build each client says it is on.
"""
from datetime import timedelta
from math import ceil

from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    APP_TYPE_MOBILE,
    APP_TYPE_TABLET,
    PLATFORM_ANDROID,
    PLATFORM_IOS,
    PLATFORM_WEB,
    UserDevice,
    VersionPolicy,
)
from .permissions import IsAdminRole
from .serializers import AdminUserDeviceSerializer, VersionPolicySerializer
from .status import filter_by_status, status_counts, thresholds
from .version_policy import active_policies, clear_policy_cache

# OS names our UA parser emits for non-mobile machines.
DESKTOP_OS_NAMES = ["Windows", "macOS", "Linux"]

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100
DEFAULT_TREND_DAYS = 14

# Only these columns may be sorted on — never interpolate user input into
# order_by(), which would expose arbitrary field/relation traversal.
DEVICE_ORDER_FIELDS = {
    "last_active",
    "last_login",
    "first_login",
    "created_at",
    "updated_at",
    "app_version",
    "build_number",
    "platform",
    "app_type",
    # Sorting by the owning user, for the Device Activity table. Explicitly
    # allow-listed relation fields only — never arbitrary traversal.
    "user__name",
    "user__username",
}


def _paginate(queryset, request):
    """Slice `queryset` per ?page/?page_size. Returns (page_qs, meta).

    Server-side by design: the device table grows one row per user per device,
    so the browser must never receive all of it (Task 11).
    """
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(request.query_params.get("page_size", DEFAULT_PAGE_SIZE))
    except (TypeError, ValueError):
        page_size = DEFAULT_PAGE_SIZE
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))

    total = queryset.count()
    total_pages = ceil(total / page_size) if total else 0
    start = (page - 1) * page_size
    return queryset[start : start + page_size], {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
    }


def _filtered_devices(request):
    """Apply every supported filter + search to the device queryset.

    `select_related` on the user (and its role) keeps the list query at a
    constant number of queries regardless of page size.
    """
    qs = UserDevice.objects.select_related("user", "user__role").all()
    params = request.query_params

    # --- exact-match filters -------------------------------------------------
    for field in ("platform", "app_type", "app_version", "browser_name", "os_name"):
        value = (params.get(field) or "").strip()
        if value:
            qs = qs.filter(**{field: value})

    build_number = (params.get("build_number") or "").strip()
    if build_number:
        try:
            qs = qs.filter(build_number=int(build_number))
        except (TypeError, ValueError):
            pass  # ignore a non-numeric build filter rather than 500

    user_id = (params.get("user_id") or "").strip()
    if user_id:
        try:
            qs = qs.filter(user_id=int(user_id))
        except (TypeError, ValueError):
            pass

    is_active = (params.get("is_active") or "").strip().lower()
    if is_active in ("true", "1"):
        qs = qs.filter(is_active=True)
    elif is_active in ("false", "0"):
        qs = qs.filter(is_active=False)

    # Derived activity status (online/idle/offline/inactive) -> a last_active
    # window. Computed, never stored; an unknown value is simply ignored.
    status_param = (params.get("status") or "").strip().lower()
    if status_param:
        qs = filter_by_status(qs, status_param, timezone.now())

    # --- date range (on last_active); an unparseable date is ignored ---------
    date_from = parse_date((params.get("date_from") or "").strip())
    if date_from:
        qs = qs.filter(last_active__date__gte=date_from)
    date_to = parse_date((params.get("date_to") or "").strip())
    if date_to:
        qs = qs.filter(last_active__date__lte=date_to)

    # --- free-text search ----------------------------------------------------
    search = (params.get("search") or "").strip()
    if search:
        qs = qs.filter(
            Q(user__name__icontains=search)
            | Q(user__username__icontains=search)
            | Q(user__email__icontains=search)
            | Q(device_id__icontains=search)
            | Q(browser_name__icontains=search)
            | Q(app_version__icontains=search)
            | Q(device_name__icontains=search)
        )

    # --- ordering (allow-listed) --------------------------------------------
    ordering = (params.get("ordering") or "-last_active").strip()
    bare = ordering.lstrip("-")
    if bare not in DEVICE_ORDER_FIELDS:
        ordering = "-last_active"
    return qs.order_by(ordering)


class AdminDeviceListView(APIView):
    """GET /api/admin/devices/ — the searchable device table."""

    permission_classes = [IsAuthenticated, IsAdminRole]

    def get(self, request):
        qs = _filtered_devices(request)
        page_qs, pagination = _paginate(qs, request)
        return Response(
            {
                "success": True,
                "message": "Devices retrieved",
                "data": {
                    "results": AdminUserDeviceSerializer(
                        page_qs,
                        many=True,
                        context={"now": timezone.now(), "policies": active_policies()},
                    ).data,
                    "pagination": pagination,
                },
            }
        )


class AdminDeviceDetailView(APIView):
    """GET /api/admin/devices/<pk>/ — one device and its user.

    Addressed by primary key, NOT by `device_id`: device_id is unique only per
    user (a shared machine legitimately yields the same device_id for two
    users), so it is not a safe lookup key on its own.
    """

    permission_classes = [IsAuthenticated, IsAdminRole]

    def get(self, request, pk):
        device = (
            UserDevice.objects.select_related("user", "user__role")
            .filter(pk=pk)
            .first()
        )
        if device is None:
            return Response(
                {"success": False, "message": "Device not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            {
                "success": True,
                "message": "Device retrieved",
                "data": AdminUserDeviceSerializer(
                    device,
                    context={"now": timezone.now(), "policies": active_policies()},
                ).data,
            }
        )


class AdminDeviceAnalyticsView(APIView):
    """GET /api/admin/devices/analytics/ — summary cards + chart series."""

    permission_classes = [IsAuthenticated, IsAdminRole]

    def get(self, request):
        now = timezone.now()
        today = now.date()
        qs = UserDevice.objects.all()

        total_devices = qs.count()
        active_devices = qs.filter(is_active=True).count()

        cards = {
            "total_devices": total_devices,
            "active_devices": active_devices,
            "inactive_devices": total_devices - active_devices,
            "mobile_devices": qs.filter(
                app_type__in=[APP_TYPE_MOBILE, APP_TYPE_TABLET]
            ).count(),
            "web_devices": qs.filter(platform=PLATFORM_WEB).count(),
            "android_devices": qs.filter(platform=PLATFORM_ANDROID).count(),
            "ios_devices": qs.filter(platform=PLATFORM_IOS).count(),
            "desktop_browsers": qs.filter(
                platform=PLATFORM_WEB, os_name__in=DESKTOP_OS_NAMES
            ).count(),
            "devices_active_today": qs.filter(last_active__date=today).count(),
        }

        # Derived activity buckets for the Device Activity summary cards. One
        # conditional-aggregation query for all four; they sum to total_devices.
        # Additive to this payload — existing consumers simply ignore them.
        cards["status_counts"] = status_counts(qs, now)
        cards["status_thresholds"] = thresholds()

        # --- version policy: latest vs old per mobile platform --------------
        # "Latest" = on the required build for that platform; "old" = a real
        # device below it. Only devices with a matching active policy are
        # classified, so platforms without a policy contribute zero to both
        # (and the web is never counted here). This feeds the four analytics
        # cards and drives the per-row Update Status column.
        policies = active_policies()
        version_policy_stats = {}
        for platform in (PLATFORM_ANDROID, PLATFORM_IOS):
            policy = policies.get(platform)
            plat_qs = qs.filter(platform=platform)
            total = plat_qs.count()
            if policy:
                required_build = policy["required_build"]
                latest = plat_qs.filter(build_number=required_build).count()
                # Anything not on the required build is "old" (older or, oddly,
                # newer). Kept simple to match the strict-equality policy rule.
                old = total - latest
            else:
                required_build = None
                latest = 0
                old = 0
            version_policy_stats[platform] = {
                "required_build": required_build,
                "required_version": policy["required_version"] if policy else None,
                "total": total,
                "latest": latest,
                "old": old,
            }
        cards["version_policy"] = version_policy_stats

        def distribution(*fields, limit=None):
            rows = (
                qs.values(*fields)
                .annotate(count=Count("id"))
                .order_by("-count")
            )
            return list(rows[:limit] if limit else rows)

        # --- version adoption: build -> device count, per mobile platform ---
        # For the "Build 5 / 12 users" chart. Ordered by build descending so the
        # newest build reads first. `required_build` is echoed so the client can
        # colour the required bar. Distinct user count so multi-device users
        # aren't double-counted in the "users" figure.
        def adoption_for(platform):
            rows = (
                qs.filter(platform=platform)
                .values("build_number")
                .annotate(
                    devices=Count("id"),
                    users=Count("user_id", distinct=True),
                )
                .order_by("-build_number")
            )
            policy = policies.get(platform)
            return {
                "required_build": policy["required_build"] if policy else None,
                "builds": [
                    {
                        "build_number": row["build_number"],
                        "devices": row["devices"],
                        "users": row["users"],
                    }
                    for row in rows
                ],
            }

        # Devices grouped by the DAY THEY WERE LAST SEEN, for the trend window.
        #
        # This is NOT a true daily-active series: `last_active` is a single
        # timestamp, so a device active on five days appears only on the last
        # one. A real DAU metric needs an append-only activity/heartbeat table
        # (see the Phase 1 `version_event` note). Labelled accordingly in the UI.
        try:
            days = max(1, min(int(request.query_params.get("days", DEFAULT_TREND_DAYS)), 90))
        except (TypeError, ValueError):
            days = DEFAULT_TREND_DAYS
        since = today - timedelta(days=days - 1)
        seen_rows = (
            qs.filter(last_active__date__gte=since)
            .annotate(day=TruncDate("last_active"))
            .values("day")
            .annotate(count=Count("id"))
            .order_by("day")
        )
        by_day = {row["day"]: row["count"] for row in seen_rows if row["day"]}
        devices_by_last_seen = [
            {
                "date": (since + timedelta(days=offset)).isoformat(),
                "count": by_day.get(since + timedelta(days=offset), 0),
            }
            for offset in range(days)
        ]

        return Response(
            {
                "success": True,
                "message": "Analytics retrieved",
                "data": {
                    "cards": cards,
                    "charts": {
                        "version_distribution": distribution(
                            "platform", "app_type", "app_version", "build_number"
                        ),
                        "platform_distribution": distribution("platform"),
                        "app_type_distribution": distribution("app_type"),
                        "browser_distribution": distribution("browser_name"),
                        "os_distribution": distribution("os_name"),
                        "devices_by_last_seen": devices_by_last_seen,
                        "version_adoption": {
                            PLATFORM_ANDROID: adoption_for(PLATFORM_ANDROID),
                            PLATFORM_IOS: adoption_for(PLATFORM_IOS),
                        },
                    },
                },
            }
        )


class AdminVersionPolicyView(APIView):
    """GET/PUT /api/admin/version-policy/ — the mobile version policies.

    GET returns both platforms as ``{ANDROID: {...}|null, IOS: {...}|null}`` so
    the admin form can load existing values in one call. PUT upserts ONE
    platform's active policy (there is at most one active row per platform).

    There is intentionally no list/history and no DELETE: this is a policy, not
    a release log. Editing a platform overwrites its single active row.
    """

    permission_classes = [IsAuthenticated, IsAdminRole]

    def get(self, request):
        existing = {
            p.platform: VersionPolicySerializer(p).data
            for p in VersionPolicy.objects.filter(
                is_active=True, platform__in=(PLATFORM_ANDROID, PLATFORM_IOS)
            )
        }
        return Response(
            {
                "success": True,
                "message": "Version policies retrieved",
                "data": {
                    PLATFORM_ANDROID: existing.get(PLATFORM_ANDROID),
                    PLATFORM_IOS: existing.get(PLATFORM_IOS),
                },
            }
        )

    def put(self, request):
        platform = str(request.data.get("platform") or "").strip().upper()
        if platform not in (PLATFORM_ANDROID, PLATFORM_IOS):
            return Response(
                {
                    "success": False,
                    "message": "platform must be ANDROID or IOS",
                    "errors": {"platform": ["Only ANDROID and IOS can be gated."]},
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Upsert the single active row for this platform.
        instance = VersionPolicy.objects.filter(
            platform=platform, is_active=True
        ).first()
        payload = {**request.data, "platform": platform, "is_active": True}
        serializer = VersionPolicySerializer(instance, data=payload)
        if not serializer.is_valid():
            return Response(
                {
                    "success": False,
                    "message": "Invalid version policy",
                    "errors": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        policy = serializer.save()
        # The middleware caches active policies; drop it so the change takes
        # effect immediately rather than after the short TTL.
        clear_policy_cache()
        return Response(
            {
                "success": True,
                "message": "Version policy saved",
                "data": VersionPolicySerializer(policy).data,
            }
        )
