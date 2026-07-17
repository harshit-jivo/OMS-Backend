"""Admin API for Device & Version Management (Phase 5).

Endpoints (all admin-only, all NEW — no existing endpoint is modified):

    GET    /api/admin/devices/            paginated + filtered + searchable list
    GET    /api/admin/devices/analytics/  cards + chart series
    GET    /api/admin/devices/<pk>/       one device + its user
    GET    /api/admin/releases/           release list (filterable)
    POST   /api/admin/releases/           create a release
    GET    /api/admin/releases/<pk>/      release detail + adoption stats
    PUT    /api/admin/releases/<pk>/      update a release
    PATCH  /api/admin/releases/<pk>/      partial update (also used to archive)

Reads the existing `devices_user_device` / `devices_app_release` tables — no new
tables. Follows the project's house style: plain `APIView`, manual query-param
handling, `{success, message, data}` envelope, aggregation via
`.values().annotate()` (the same shape as `orders.views.DashboardChartsView`).

Scope: this phase is read/report + release CRUD only. No force-update decision
is computed anywhere here — "outdated" counts are analytics, not enforcement.
"""
from datetime import timedelta
from math import ceil

from django.db import transaction
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
    AppRelease,
    PLATFORM_ANDROID,
    PLATFORM_IOS,
    PLATFORM_WEB,
    UserDevice,
)
from .permissions import IsAdminRole
from .serializers import AdminUserDeviceSerializer, AppReleaseSerializer
from .status import filter_by_status, status_counts, thresholds

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
                        page_qs, many=True, context={"now": timezone.now()}
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
                    device, context={"now": timezone.now()}
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

        # Latest release per product, and how many devices trail it. "Outdated"
        # here is a REPORT, not an enforcement decision.
        latest_releases = list(
            AppRelease.objects.filter(is_latest=True, is_active=True).values(
                "platform", "app_type", "version", "build_number"
            )
        )
        outdated_devices = 0
        on_latest_devices = 0
        for release in latest_releases:
            product = qs.filter(
                platform=release["platform"], app_type=release["app_type"]
            )
            outdated_devices += product.filter(
                build_number__lt=release["build_number"]
            ).count()
            on_latest_devices += product.filter(
                build_number=release["build_number"]
            ).count()

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
            "outdated_devices": outdated_devices,
            "on_latest_devices": on_latest_devices,
            "latest_releases": latest_releases,
        }

        # Derived activity buckets for the Device Activity summary cards. One
        # conditional-aggregation query for all four; they sum to total_devices.
        # Additive to this payload — existing consumers simply ignore them.
        cards["status_counts"] = status_counts(qs, now)
        cards["status_thresholds"] = thresholds()

        def distribution(*fields, limit=None):
            rows = (
                qs.values(*fields)
                .annotate(count=Count("id"))
                .order_by("-count")
            )
            return list(rows[:limit] if limit else rows)

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
                    },
                },
            }
        )


def _clear_other_latest(platform, app_type, exclude_pk=None):
    """Demote any other 'latest' release for this product.

    The DB enforces at most one `is_latest` per (platform, app_type) via the
    partial unique index `apprelease_one_latest_uq`. That constraint REJECTS a
    second latest rather than swapping, so the publisher must clear the old flag
    in the same transaction — this is that step.
    """
    others = AppRelease.objects.filter(
        platform=platform, app_type=app_type, is_latest=True
    )
    if exclude_pk is not None:
        others = others.exclude(pk=exclude_pk)
    others.update(is_latest=False)


class AdminReleaseListCreateView(APIView):
    """GET/POST /api/admin/releases/ — list and create version-policy rows."""

    permission_classes = [IsAuthenticated, IsAdminRole]

    def get(self, request):
        qs = AppRelease.objects.all()
        for field in ("platform", "app_type"):
            value = (request.query_params.get(field) or "").strip()
            if value:
                qs = qs.filter(**{field: value})
        is_active = (request.query_params.get("is_active") or "").strip().lower()
        if is_active in ("true", "1"):
            qs = qs.filter(is_active=True)
        elif is_active in ("false", "0"):
            qs = qs.filter(is_active=False)

        qs = qs.order_by("platform", "app_type", "-build_number")
        page_qs, pagination = _paginate(qs, request)
        return Response(
            {
                "success": True,
                "message": "Releases retrieved",
                "data": {
                    "results": AppReleaseSerializer(page_qs, many=True).data,
                    "pagination": pagination,
                },
            }
        )

    def post(self, request):
        serializer = AppReleaseSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {
                    "success": False,
                    "message": "Invalid release data",
                    "errors": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = serializer.validated_data
        with transaction.atomic():
            if data.get("is_latest"):
                _clear_other_latest(data["platform"], data["app_type"])
            release = serializer.save()

        return Response(
            {
                "success": True,
                "message": "Release created",
                "data": AppReleaseSerializer(release).data,
            },
            status=status.HTTP_201_CREATED,
        )


class AdminReleaseDetailView(APIView):
    """GET/PUT/PATCH /api/admin/releases/<pk>/.

    PATCH doubles as "archive" (`{"is_active": false}`). There is deliberately
    no DELETE: releases are history, and a deleted policy row would silently
    change what clients are told.
    """

    permission_classes = [IsAuthenticated, IsAdminRole]

    def _get(self, pk):
        return AppRelease.objects.filter(pk=pk).first()

    def get(self, request, pk):
        release = self._get(pk)
        if release is None:
            return Response(
                {"success": False, "message": "Release not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # --- adoption for this exact build ----------------------------------
        product_devices = UserDevice.objects.filter(
            platform=release.platform, app_type=release.app_type
        )
        total_for_product = product_devices.count()
        on_this_build = product_devices.filter(build_number=release.build_number)
        device_count = on_this_build.count()
        user_count = on_this_build.values("user_id").distinct().count()
        adoption_percent = (
            round(device_count * 100.0 / total_for_product, 2)
            if total_for_product
            else 0.0
        )

        # --- neighbouring releases (same product, by build number) -----------
        siblings = AppRelease.objects.filter(
            platform=release.platform, app_type=release.app_type
        )
        previous = (
            siblings.filter(build_number__lt=release.build_number)
            .order_by("-build_number")
            .first()
        )
        following = (
            siblings.filter(build_number__gt=release.build_number)
            .order_by("build_number")
            .first()
        )

        def brief(item):
            if item is None:
                return None
            return {
                "id": item.id,
                "version": item.version,
                "build_number": item.build_number,
                "released_at": item.released_at,
            }

        return Response(
            {
                "success": True,
                "message": "Release retrieved",
                "data": {
                    "release": AppReleaseSerializer(release).data,
                    "adoption": {
                        "device_count": device_count,
                        "user_count": user_count,
                        "adoption_percent": adoption_percent,
                        "total_devices_for_product": total_for_product,
                    },
                    "previous_release": brief(previous),
                    "next_release": brief(following),
                },
            }
        )

    def _update(self, request, pk, partial):
        release = self._get(pk)
        if release is None:
            return Response(
                {"success": False, "message": "Release not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = AppReleaseSerializer(release, data=request.data, partial=partial)
        if not serializer.is_valid():
            return Response(
                {
                    "success": False,
                    "message": "Invalid release data",
                    "errors": serializer.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = serializer.validated_data
        with transaction.atomic():
            wants_latest = data.get("is_latest", release.is_latest)
            if wants_latest:
                _clear_other_latest(
                    data.get("platform", release.platform),
                    data.get("app_type", release.app_type),
                    exclude_pk=release.pk,
                )
            updated = serializer.save()

        return Response(
            {
                "success": True,
                "message": "Release updated",
                "data": AppReleaseSerializer(updated).data,
            }
        )

    def put(self, request, pk):
        return self._update(request, pk, partial=False)

    def patch(self, request, pk):
        return self._update(request, pk, partial=True)
