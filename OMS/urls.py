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

    path('api/auth/', include('users.urls')),
    path('api/orders/', include('orders.urls')),
    path('api/sap/', include('sap_sync.urls')),
    path('api/hana/', include('hana.urls')),
    path('api/sku/', include('SKU.urls')),
    path('api/service-layer/' , include('serviceLayer.urls')),
    path('api/einvoice/', include('einvoice.urls')),
    path('api/ewaybill/', include('ewaybill.urls')),
    path('api/invoice/', include('invoice.urls')),
    path('api/legal/' , include('legal.urls')),
    # Device tracking — routes are /api/devices/... and /api/admin/devices/...
    # (paths declared explicitly inside devices/urls.py).
    path('api/', include('devices.urls')),
    path('api/tracker/', include('tracker.urls')),
    # Dynamic UI labels: /api/ui-config/labels/ (public read) + admin CRUD.
    path('api/ui-config/', include('uilabels.urls')),
    path('api/payments/', include('payments.urls')),
    path('api/approvals/', include('approvals.urls')),
    # Reusable notification framework read API (Payment/Deposit/future modules).
    # Separate from /api/orders/notifications/ (old Orders system, untouched).
    path('api/notifications/', include('notifications.urls')),
    # HAIS — hardware asset register (own `hais` Postgres schema).
    path('api/hais/', include('HAIS.urls')),
]


if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)