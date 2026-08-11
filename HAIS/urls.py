"""HAIS routes. Mounted at ``api/hais/`` in the project URLconf.

  /api/hais/asset-types/     dynamic dropdown (CRUD)
  /api/hais/departments/     dynamic dropdown (CRUD)
  /api/hais/storage-types/   dynamic dropdown (CRUD)
  /api/hais/assets/          asset register (CRUD, keyed by Asset ID)
  /api/hais/assets/by-serial/?code=...   resolve a scanned QR to a device
"""
from rest_framework.routers import DefaultRouter

from .views import (
    AssetTypeViewSet,
    AssetViewSet,
    DepartmentViewSet,
    StorageTypeViewSet,
)

router = DefaultRouter()
router.register(r"asset-types", AssetTypeViewSet, basename="hais-asset-type")
router.register(r"departments", DepartmentViewSet, basename="hais-department")
router.register(r"storage-types", StorageTypeViewSet, basename="hais-storage-type")
router.register(r"assets", AssetViewSet, basename="hais-asset")

urlpatterns = router.urls
