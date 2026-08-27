"""Health and readiness routes — Phase 5.3.

Mounted at `/api/health/` from the project URLconf. `core` had no URLs before
this; it held shared primitives only.
"""
from django.urls import path

from core.health import HealthDetailView, LivenessView, ReadinessView

urlpatterns = [
    path('live/', LivenessView.as_view(), name='health-live'),
    path('ready/', ReadinessView.as_view(), name='health-ready'),
    path('detail/', HealthDetailView.as_view(), name='health-detail'),
]
