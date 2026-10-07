"""Credit Limit routes, mounted at ``api/credit-limit/``.

Requester (`Credit_Limit`):
  GET        customers/<card_code>/?company=   live OCRD credit facts
  GET POST   requests/                         ?company=&status=
Either key (own requests, or any for an approver):
  GET        requests/<pk>/   requests/<pk>/history/   requests/<pk>/attachments/<id>/
Approver (`Credit_Limit_Approval` AND the current stage's effective user):
  GET        approvals/queue/   approvals/history/?status=
  POST       requests/<pk>/approve/   requests/<pk>/reject/
"""
from django.urls import path

from . import views

urlpatterns = [
    path('customers/<str:card_code>/', views.CustomerView.as_view(),
         name='credit-limit-customer'),
    path('requests/', views.RequestListCreateView.as_view(),
         name='credit-limit-request-list'),
    path('requests/<int:pk>/', views.RequestDetailView.as_view(),
         name='credit-limit-request-detail'),
    path('requests/<int:pk>/history/', views.RequestHistoryView.as_view(),
         name='credit-limit-request-history'),
    path('requests/<int:pk>/attachments/<int:attachment_id>/',
         views.AttachmentView.as_view(),
         name='credit-limit-request-attachment'),
    path('requests/<int:pk>/approve/', views.ApproveView.as_view(),
         name='credit-limit-request-approve'),
    path('requests/<int:pk>/reject/', views.RejectView.as_view(),
         name='credit-limit-request-reject'),
    path('approvals/queue/', views.ApprovalQueueView.as_view(),
         name='credit-limit-approval-queue'),
    path('approvals/history/', views.ApprovalHistoryView.as_view(),
         name='credit-limit-approval-history'),
]
