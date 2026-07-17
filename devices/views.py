"""API views for device & app-version management (Phase 2).

Endpoints (all under /api/):

    POST   /api/devices/register/   IsAuthenticated   upsert the caller's device
    PUT    /api/devices/update/     IsAuthenticated   refresh device telemetry
    GET    /api/devices/me/         IsAuthenticated   list the caller's devices
    GET    /api/app/version/        AllowAny          version policy for a product

Response envelope matches the rest of the project: ``{success, message, data}``
on success, ``{success: False, message, errors}`` on validation failure.

Scope note for this phase: registration is NOT wired into the login flow, no
force-update decision is computed, and no existing endpoint is touched. The
version endpoint reports policy data only.
"""
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AppRelease, PLATFORM_WEB
from .serializers import (
    AppVersionQuerySerializer,
    DeviceRegisterSerializer,
    DeviceUpdateSerializer,
    UserDeviceSerializer,
)
from .services import (
    list_devices,
    register_device,
    update_device,
)
from .utils import parse_browser, parse_os


class DeviceRegisterView(APIView):
    """POST /api/devices/register/ — idempotent upsert of the caller's device.

    The user comes from the JWT; the device is identified by the client-supplied
    ``device_id``, scoped to that user. Browser/OS for web clients are derived
    from the User-Agent server-side, never trusted from the body.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = DeviceRegisterSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"success": False, "message": "Invalid device data", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = dict(serializer.validated_data)

        # Derive browser (web only) and fall back to UA-derived OS if the client
        # didn't send one. Native clients report os_name/os_version explicitly.
        user_agent = request.META.get("HTTP_USER_AGENT", "")
        if data["platform"] == PLATFORM_WEB:
            data["browser_name"], data["browser_version"] = parse_browser(user_agent)
            if not data.get("os_name"):
                data["os_name"], data["os_version"] = parse_os(user_agent)

        device, created = register_device(request.user, data)
        return Response(
            {
                "success": True,
                "message": "Device registered",
                "data": UserDeviceSerializer(device).data,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class DeviceUpdateView(APIView):
    """PUT /api/devices/update/ — refresh telemetry for an existing device.

    404s if the caller has no device with that id (registration is the only
    creator). Only ever addresses the caller's own device, so there is no IDOR
    surface even though ``device_id`` is client-supplied.
    """

    permission_classes = [IsAuthenticated]

    def put(self, request):
        serializer = DeviceUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"success": False, "message": "Invalid device data", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = dict(serializer.validated_data)
        device_id = data.pop("device_id")

        device = update_device(request.user, device_id, data)
        if device is None:
            return Response(
                {"success": False, "message": "Device not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {
                "success": True,
                "message": "Device updated",
                "data": UserDeviceSerializer(device).data,
            }
        )


class CurrentDevicesView(APIView):
    """GET /api/devices/me/ — the caller's registered devices.

    Returns all of the caller's active devices (newest-active first). Pass
    ``?device_id=<id>`` to fetch just one (the "current device" case).
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        device_id = (request.query_params.get("device_id") or "").strip()
        devices = list_devices(request.user)
        if device_id:
            devices = devices.filter(device_id=device_id)

        return Response(
            {
                "success": True,
                "message": "Devices retrieved",
                "data": UserDeviceSerializer(devices, many=True).data,
            }
        )


class AppVersionView(APIView):
    """GET /api/app/version/ — version policy for a platform + app_type.

    AllowAny by design: the check must work before authentication (a client with
    an expired token still needs to learn it should update). Reports policy data
    only — this phase computes NO force-update decision. ``update_available`` is
    an informational flag (a newer build exists), not an enforcement signal.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        serializer = AppVersionQuerySerializer(data=request.query_params)
        if not serializer.is_valid():
            return Response(
                {"success": False, "message": "Invalid query", "errors": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        params = serializer.validated_data
        release = (
            AppRelease.objects.filter(
                platform=params["platform"],
                app_type=params["app_type"],
                is_latest=True,
                is_active=True,
            )
            .order_by("-build_number")
            .first()
        )

        if release is None:
            # No policy configured yet — a well-formed "nothing to report".
            return Response(
                {
                    "success": True,
                    "message": "No release configured for this platform/app_type",
                    "data": None,
                }
            )

        client_build = params.get("build_number")
        update_available = (
            client_build is not None and client_build < release.build_number
        )

        return Response(
            {
                "success": True,
                "message": "Latest release",
                "data": {
                    "platform": release.platform,
                    "app_type": release.app_type,
                    "latest_version": release.version,
                    "latest_build_number": release.build_number,
                    "min_supported_version": release.min_supported_version,
                    "min_supported_build": release.min_supported_build,
                    "release_notes": release.release_notes,
                    "store_url": release.store_url,
                    "released_at": release.released_at,
                    "update_available": update_available,
                    # NOTE: no `update_required` / force-update decision here.
                    # Enforcement is a later phase; this endpoint reports data.
                },
            }
        )
