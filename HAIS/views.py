"""HAIS API — dynamic dropdown masters + the asset register with its log.

All endpoints require authentication (JWT, project default). The dropdown
masters are full CRUD so the lists stay DB-driven; ``?active=1`` returns only
active rows for populating a form dropdown.
"""
from django.db.models import Q
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .models import Asset, AssetType, Department, StorageType
from .serializers import (
    AssetSerializer,
    AssetTypeSerializer,
    DepartmentSerializer,
    PublicAssetSerializer,
    StorageTypeSerializer,
)


class _MasterViewSet(viewsets.ModelViewSet):
    """Shared behaviour for the dropdown master endpoints."""

    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = self.queryset
        if self.request.query_params.get("active") in {"1", "true", "yes"}:
            qs = qs.filter(is_active=True)
        return qs


class AssetTypeViewSet(_MasterViewSet):
    queryset = AssetType.objects.all()
    serializer_class = AssetTypeSerializer


class DepartmentViewSet(_MasterViewSet):
    queryset = Department.objects.all()
    serializer_class = DepartmentSerializer


class StorageTypeViewSet(_MasterViewSet):
    queryset = StorageType.objects.all()
    serializer_class = StorageTypeSerializer


class AssetViewSet(viewsets.ModelViewSet):
    """The asset register. Keyed by Asset ID; carries the full history log."""

    queryset = Asset.objects.all().prefetch_related("storage_types", "logs")
    serializer_class = AssetSerializer
    permission_classes = [IsAuthenticated]
    lookup_field = "asset_id"
    lookup_value_regex = "[^/]+"  # Asset IDs contain hyphens

    def get_queryset(self):
        qs = super().get_queryset()
        p = self.request.query_params
        search = p.get("search")
        if search:
            qs = qs.filter(
                Q(asset_id__icontains=search)
                | Q(serial_num__icontains=search)
                | Q(current_user_name__icontains=search)
                | Q(current_user_id__icontains=search)
                | Q(prev_user_name__icontains=search)
                | Q(company__icontains=search)
                | Q(model_num__icontains=search)
                | Q(current_location__icontains=search)
            )
        if p.get("working_status"):
            qs = qs.filter(working_status=p["working_status"])
        if p.get("department"):
            qs = qs.filter(department_id=p["department"])
        if p.get("asset_type"):
            qs = qs.filter(asset_type_id=p["asset_type"])
        return qs

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        # Optional "reason" captured on update (handover / config change).
        if self.request.method in ("PUT", "PATCH"):
            ctx["reason"] = self.request.data.get("reason", "")
        return ctx

    @action(detail=False, methods=["get"], url_path="by-serial")
    def by_serial(self, request):
        """Resolve a scanned QR (the serial number) to a device.

        Falls back to Asset ID so an older ID-based code still resolves.
        """
        code = (request.query_params.get("code") or "").strip()
        if not code:
            return Response({"detail": "code is required."}, status=400)
        asset = (
            self.get_queryset()
            .filter(Q(serial_num__iexact=code) | Q(asset_id__iexact=code))
            .first()
        )
        if not asset:
            return Response(
                {"detail": f'No device matches the scanned code "{code}".'}, status=404
            )
        return Response(self.get_serializer(asset).data)


class PublicAssetByCodeView(APIView):
    """Resolve a scanned QR to a device, WITHOUT a session.

    The point of the sticker: it is scanned with whatever phone is to hand,
    usually by someone who has no OMS account and never will. Requiring a
    login here made the QR useless to everyone except the team that already
    has the register open.

    THREE THINGS KEEP THIS SAFE, and all three are load bearing.

    1. `PublicAssetSerializer`, an allow-list. See its docstring for what is
       held back and why.

    2. SERIAL NUMBER ONLY. The authenticated `by-serial` also accepts an Asset
       ID, and Asset IDs are SEQUENTIAL — `_next_asset_id` hands out
       JIVO-LAP-0001, 0002, 0003. Accepting them here would let anyone walk
       the range and dump the register, one employee's name and email at a
       time. A serial number is a manufacturer string; it cannot be guessed,
       only read off a device you are holding.

    3. Its own throttle scope. Serial numbers are not guessable but they are
       not secret either, and a scraper with a list of them should not get the
       whole staff directory in one pass.
    """

    permission_classes = [AllowAny]
    authentication_classes = []  # No session is involved; never 401 a scanner.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "hais_public_device"

    def get(self, request):
        code = (request.query_params.get("code") or "").strip()
        if not code:
            return Response({"detail": "code is required."}, status=400)

        asset = (
            Asset.objects.select_related("asset_type", "department")
            .prefetch_related("storage_types")
            .filter(serial_num__iexact=code)
            .first()
        )
        if not asset:
            # The same message whether the serial is unknown or malformed:
            # confirming that a serial EXISTS is itself a small leak.
            return Response(
                {"detail": f'No device matches the scanned code "{code}".'}, status=404
            )
        return Response(PublicAssetSerializer(asset).data)
