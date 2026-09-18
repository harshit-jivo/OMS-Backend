"""BackDate (BKDT) routes. Mounted at ``api/backdate/`` and ``api/v1/backdate/``.

Requester (`BackDate`):
  GET  POST  /api/backdate/requests/            ?status=&company=&search=&month=MM-YYYY
  GET PATCH  /api/backdate/requests/<pk>/       PATCH edits a pending request
  GET        /api/backdate/requests/<pk>/history/  actions + stage progress
  GET        /api/backdate/insights/            ?company=&month=MM-YYYY
  GET        /api/backdate/sap-users/           ?company=OIL
  GET        /api/backdate/document-types/      ?company=OIL

Approver (`BackDate_Approval` AND the effective stage user):
  GET        /api/backdate/approvals/queue/     ?company=&search=
  GET        /api/backdate/approvals/history/   ?status=&company=&search=
  GET        /api/backdate/approvals/insights/  ?company=&search=
  POST       /api/backdate/requests/<pk>/approve/
  POST       /api/backdate/requests/<pk>/reject/
  POST       /api/backdate/requests/<pk>/retry-hana/

DECISIONS ARE ADDRESSED BY REQUEST, not by task. There is no task table: a
request waits at one stage at a time, and the flow says which — so "approve
request 55" is unambiguous and needs no second identifier for the client to
track. The earlier `/tasks/<pk>/approve/` pair is gone with it.

NOT migrated from JSAP, deliberately:
  POST /SaveBKDT          — wrote arbitrary rights to HANA with no request,
                            no approval and no authentication
  POST /UpdateHanaStatus  — let any caller rewrite the applied-rights audit
                            flag; the module writes it now
  POST /BackDateSaveInHana?flowId=  — replaced by `retry-hana/`, which requires
                            the approval key and a fully approved flow
"""
from django.urls import path

from . import views

urlpatterns = [
    # --- SAP master data -------------------------------------------------
    path('sap-users/', views.SapUserListView.as_view(),
         name='backdate-sap-users'),
    path('document-types/', views.DocumentTypeListView.as_view(),
         name='backdate-document-types'),

    # --- requests --------------------------------------------------------
    path('requests/', views.RequestListCreateView.as_view(),
         name='backdate-request-list'),
    path('requests/<int:pk>/', views.RequestDetailView.as_view(),
         name='backdate-request-detail'),
    path('requests/<int:pk>/history/', views.RequestHistoryView.as_view(),
         name='backdate-request-history'),
    path('requests/<int:pk>/approve/', views.RequestApproveView.as_view(),
         name='backdate-request-approve'),
    path('requests/<int:pk>/reject/', views.RequestRejectView.as_view(),
         name='backdate-request-reject'),
    path('requests/<int:pk>/retry-hana/', views.RetryHanaView.as_view(),
         name='backdate-retry-hana'),
    path('insights/', views.InsightsView.as_view(), name='backdate-insights'),

    # --- approval desk ---------------------------------------------------
    path('approvals/queue/', views.ApprovalQueueView.as_view(),
         name='backdate-approval-queue'),
    path('approvals/history/', views.ApprovalHistoryView.as_view(),
         name='backdate-approval-history'),
    path('approvals/insights/', views.ApprovalInsightsView.as_view(),
         name='backdate-approval-insights'),
]
