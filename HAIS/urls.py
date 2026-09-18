"""HAIS routes. Mounted at ``api/hais/`` in the project URLconf.

  /api/hais/asset-types/     dynamic dropdown (CRUD)
  /api/hais/departments/     dynamic dropdown (CRUD)
  /api/hais/storage-types/   dynamic dropdown (CRUD)
  /api/hais/assets/          asset register (CRUD, keyed by Asset ID)
  /api/hais/assets/by-serial/?code=...   resolve a scanned QR to a device
  /api/hais/public/device/?code=...      the same, PUBLIC and narrowed
"""
from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    AssetTypeViewSet,
    AssetViewSet,
    DepartmentViewSet,
    PublicAssetByCodeView,
    StorageTypeViewSet,
)

router = DefaultRouter()
router.register(r"asset-types", AssetTypeViewSet, basename="hais-asset-type")
router.register(r"departments", DepartmentViewSet, basename="hais-department")
router.register(r"storage-types", StorageTypeViewSet, basename="hais-storage-type")
router.register(r"assets", AssetViewSet, basename="hais-asset")

urlpatterns = [
    # Before the router: an explicit path, so it can never be shadowed by a
    # viewset lookup. Unauthenticated by design — see the view.
    path("public/device/", PublicAssetByCodeView.as_view(), name="hais-public-device"),
] + router.urls
