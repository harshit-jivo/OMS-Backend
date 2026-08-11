"""HAIS API — dynamic dropdown masters + the asset register with its log.

All endpoints require authentication (JWT, project default). The dropdown
masters are full CRUD so the lists stay DB-driven; ``?active=1`` returns only
active rows for populating a form dropdown.
"""
from django.db.models import Q
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Asset, AssetType, Department, StorageType
from .serializers import (
    AssetSerializer,
    AssetTypeSerializer,
    DepartmentSerializer,
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
