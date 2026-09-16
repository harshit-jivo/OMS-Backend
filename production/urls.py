"""PRDO (Production Order) routes. Mounted at ``api/production/``.

Viewer (`Production_Order`):
  GET   /api/production/requests/             ?status=&company=&item_code=
  GET   /api/production/requests/<pk>/
  GET   /api/production/requests/<pk>/history/
  GET   /api/production/insights/             ?company=
  GET   /api/production/health/

Approver (`Production_Order_Approval` AND the effective stage user):
  GET   /api/production/approvals/queue/      ?company=
  GET   /api/production/approvals/history/    ?status=&company=
  POST  /api/production/requests/<pk>/approve/
  POST  /api/production/requests/<pk>/reject/
  POST  /api/production/requests/<pk>/retry-sap/

THERE IS NO POST /requests/ AND NO PATCH. SAP is the point of origin: a
production order enters OMS only through `manage.py sync_production_orders`.
OMS notices, decides, and tells SAP — it never creates, edits, cancels or
closes a production order.

DECISIONS ARE ADDRESSED BY REQUEST, not by task. There is no task table: a
request waits at one stage at a time and the flow says which, so "approve
request 55" is unambiguous.
"""
from django.urls import path

from . import views

urlpatterns = [
    # --- requests --------------------------------------------------------
    path('requests/', views.RequestListView.as_view(),
         name='production-request-list'),
    path('requests/<int:pk>/', views.RequestDetailView.as_view(),
         name='production-request-detail'),
    path('requests/<int:pk>/history/', views.RequestHistoryView.as_view(),
         name='production-request-history'),
    path('requests/<int:pk>/approve/', views.RequestApproveView.as_view(),
         name='production-request-approve'),
    path('requests/<int:pk>/reject/', views.RequestRejectView.as_view(),
         name='production-request-reject'),
    path('requests/<int:pk>/retry-sap/', views.RetrySapView.as_view(),
         name='production-retry-sap'),

    # --- approval desk ---------------------------------------------------
    path('approvals/queue/', views.ApprovalQueueView.as_view(),
         name='production-approval-queue'),
    path('approvals/history/', views.ApprovalHistoryView.as_view(),
         name='production-approval-history'),

    # --- reporting -------------------------------------------------------
    path('insights/', views.InsightsView.as_view(), name='production-insights'),
    path('health/', views.HealthView.as_view(), name='production-health'),
]
