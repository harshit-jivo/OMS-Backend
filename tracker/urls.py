from django.urls import path

from .admin_views import (
    LookupAdminDetail, LookupAdminListCreate, StageAdminDetail,
    StageAdminListCreate, TrackerUserDetail, TrackerUserListCreate,
    UserStageListView, UserStageSetView,
)
from .views import (
    AdminInvoicesExportView, AdminInvoicesView, AlertsView, BulkActionView,
    InvoiceDetailView, InvoiceListCreateView, JsapStatusView, JsapSyncView,
    LookupsView, MyQueueView, PaymentDetailView, ReportsView,
    StageAdvancedView, VendorsView,
)

urlpatterns = [
    path('lookups/', LookupsView.as_view(), name='tracker-lookups'),
    path('vendors/', VendorsView.as_view(), name='tracker-vendors'),
    path('invoices/', InvoiceListCreateView.as_view(), name='tracker-invoices'),
    path('invoices/<int:pk>/', InvoiceDetailView.as_view(), name='tracker-invoice-detail'),
    path('invoices/<int:pk>/payment/', PaymentDetailView.as_view(), name='tracker-invoice-payment'),
    path('invoices/<int:pk>/jsap/', JsapStatusView.as_view(), name='tracker-invoice-jsap'),
    path('jsap/sync/', JsapSyncView.as_view(), name='tracker-jsap-sync'),
    path('my-queue/', MyQueueView.as_view(), name='tracker-my-queue'),
    path('stage-advanced/', StageAdvancedView.as_view(), name='tracker-stage-advanced'),
    path('actions/bulk/', BulkActionView.as_view(), name='tracker-bulk-action'),
    path('reports/', ReportsView.as_view(), name='tracker-reports'),
    path('alerts/', AlertsView.as_view(), name='tracker-alerts'),
    path('all-invoices/', AdminInvoicesView.as_view(), name='tracker-all-invoices'),
    path('all-invoices/export/', AdminInvoicesExportView.as_view(), name='tracker-all-invoices-export'),

    # --- Admin / config (IsTrackerAdmin) ---
    path('admin/stages/', StageAdminListCreate.as_view(), name='tracker-admin-stages'),
    path('admin/stages/<int:pk>/', StageAdminDetail.as_view(), name='tracker-admin-stage'),
    path('admin/lookups/<str:kind>/', LookupAdminListCreate.as_view(), name='tracker-admin-lookups'),
    path('admin/lookups/<str:kind>/<int:pk>/', LookupAdminDetail.as_view(), name='tracker-admin-lookup'),
    path('admin/users/', UserStageListView.as_view(), name='tracker-admin-users'),
    path('admin/users/<int:user_id>/stages/', UserStageSetView.as_view(), name='tracker-admin-user-stages'),
    # Tracker user CRUD (create / list / delete tracker users only)
    path('admin/tracker-users/', TrackerUserListCreate.as_view(), name='tracker-admin-tracker-users'),
    path('admin/tracker-users/<int:user_id>/', TrackerUserDetail.as_view(), name='tracker-admin-tracker-user'),
]
