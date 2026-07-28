"""API views for device tracking.

Endpoints (all under /api/):

    POST   /api/devices/register/   IsAuthenticated   upsert the caller's device
    PUT    /api/devices/update/     IsAuthenticated   refresh device telemetry
    GET    /api/devices/me/         IsAuthenticated   list the caller's devices

Response envelope matches the rest of the project: ``{success, message, data}``
on success, ``{success: False, message, errors}`` on validation failure.

These endpoints only ever RECORD what a client reports about itself. There is
no version policy to serve: the deployed app is the source of truth for its own
version and build, so nothing here tells a client what it should be running.
"""
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import PLATFORM_WEB
from .serializers import (
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
