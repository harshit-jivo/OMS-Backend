"""
URL configuration for OMS project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path,include
from django.conf import settings
from django.conf.urls.static import static
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
)

# ---------------------------------------------------------------------------
# The API surface, declared ONCE and mounted twice — Phase 6.2.
#
# Every entry here is reachable at two prefixes:
#
#     /api/<app>/...        the paths every existing client calls today
#     /api/v1/<app>/...     the same routes, under an explicit version
#
# Versioning had to happen BEFORE any response shape changes, and it had to
# happen without breaking the live web and mobile clients — so the versioned
# prefix is added alongside rather than replacing. Nothing about the
# unversioned paths changes: same views, same permissions, same responses.
#
# The value is that there is now somewhere to put a v2. Today a response shape
# cannot be changed at all without breaking whichever client has not shipped
# yet; from here, v1 keeps its shape and the new one lands beside it.
#
# The v1 mount is NAMESPACED. Including the same patterns twice registers every
# route name twice, and `reverse('health-live')` would then resolve to whichever
# was registered last — silently changing what it returns. With a namespace,
# `reverse('health-live')` still means the unversioned path and
# `reverse('v1:health-live')` the versioned one, so nothing moves under an
# existing caller.
# ---------------------------------------------------------------------------
api_urlpatterns = [
    # Liveness / readiness / diagnosis. The first two answer anonymously so a
    # load balancer can reach them; see core/health.py for why they say so
    # little and why a HANA outage is not a 503.
    path('health/', include('core.urls')),

    path('auth/', include('users.urls')),
    path('orders/', include('orders.urls')),
    path('sap/', include('sap_sync.urls')),
    path('hana/', include('hana.urls')),
    path('sku/', include('SKU.urls')),
    path('service-layer/', include('serviceLayer.urls')),
    path('einvoice/', include('einvoice.urls')),
    path('ewaybill/', include('ewaybill.urls')),
    path('invoice/', include('invoice.urls')),
    path('legal/', include('legal.urls')),
    # Device tracking — routes are devices/... and admin/devices/...
    # (paths declared explicitly inside devices/urls.py).
    path('', include('devices.urls')),
    path('tracker/', include('tracker.urls')),
    # Dynamic UI labels: ui-config/labels/ (public read) + admin CRUD.
    path('ui-config/', include('uilabels.urls')),
    path('payments/', include('payments.urls')),
    path('approvals/', include('approvals.urls')),
    # Reusable notification framework read API (Payment/Deposit/future
    # modules). Separate from orders/notifications/ (old Orders system).
    path('notifications/', include('notifications.urls')),
    # HAIS — hardware asset register (own `hais` Postgres schema).
    path('hais/', include('HAIS.urls')),
    # Generic workflow engine (own `workflow` Postgres schema). Separate from
    # approvals/, which continues to serve PAYMENT and DEPOSIT.
    path('workflow/', include('workflow.urls')),
    # BackDate (BKDT) — back-posting rights, own `backdate` Postgres
    # schema. Drives the generic workflow engine; owns its own runtime.
    path('backdate/', include('backdate.urls')),
]

urlpatterns = [
    path('admin/', admin.site.urls),

    # OpenAPI 3 schema and the two browsable renderings of it. Authenticated
    # only, via SPECTACULAR_SETTINGS['SERVE_PERMISSIONS'] — these do NOT
    # inherit DEFAULT_PERMISSION_CLASSES, and default to AllowAny without it.
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/schema/swagger-ui/',
         SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/schema/redoc/',
         SpectacularRedocView.as_view(url_name='schema'), name='redoc'),

    # The canonical, documented prefix. This is what the published OpenAPI
    # schema describes and what new clients should call.
    path('api/v1/', include((api_urlpatterns, 'v1'), namespace='v1')),

    # The unversioned prefix, kept working indefinitely for the clients that
    # already ship against it. Deliberately listed SECOND so that a duplicated
    # route name resolves here by default — see the namespace note above.
    path('api/', include(api_urlpatterns)),
]


if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)