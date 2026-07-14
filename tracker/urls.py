from django.urls import path

from .admin_views import (
    LookupAdminDetail, LookupAdminListCreate, StageAdminDetail,
    StageAdminListCreate, UserStageListView, UserStageSetView,
)
from .views import (
    AlertsView, BulkActionView, InvoiceDetailView, InvoiceListCreateView,
    LookupsView, MyQueueView, PaymentDetailView, ReportsView, StageAdvancedView,
)

urlpatterns = [
    path('lookups/', LookupsView.as_view(), name='tracker-lookups'),
    path('invoices/', InvoiceListCreateView.as_view(), name='tracker-invoices'),
    path('invoices/<int:pk>/', InvoiceDetailView.as_view(), name='tracker-invoice-detail'),
    path('invoices/<int:pk>/payment/', PaymentDetailView.as_view(), name='tracker-invoice-payment'),
    path('my-queue/', MyQueueView.as_view(), name='tracker-my-queue'),
    path('stage-advanced/', StageAdvancedView.as_view(), name='tracker-stage-advanced'),
    path('actions/bulk/', BulkActionView.as_view(), name='tracker-bulk-action'),
    path('reports/', ReportsView.as_view(), name='tracker-reports'),
    path('alerts/', AlertsView.as_view(), name='tracker-alerts'),

    # --- Admin / config (IsTrackerAdmin) ---
    path('admin/stages/', StageAdminListCreate.as_view(), name='tracker-admin-stages'),
    path('admin/stages/<int:pk>/', StageAdminDetail.as_view(), name='tracker-admin-stage'),
    path('admin/lookups/<str:kind>/', LookupAdminListCreate.as_view(), name='tracker-admin-lookups'),
    path('admin/lookups/<str:kind>/<int:pk>/', LookupAdminDetail.as_view(), name='tracker-admin-lookup'),
    path('admin/users/', UserStageListView.as_view(), name='tracker-admin-users'),
    path('admin/users/<int:user_id>/stages/', UserStageSetView.as_view(), name='tracker-admin-user-stages'),
]
