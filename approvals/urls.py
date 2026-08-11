from django.urls import path

from . import views

urlpatterns = [
    # Approver-facing
    path('inbox/', views.InboxView.as_view(), name='approval-inbox'),
    path('requests/', views.ApprovalRequestListView.as_view(), name='approval-request-list'),
    path('requests/<int:pk>/', views.ApprovalRequestDetailView.as_view(),
         name='approval-request-detail'),
    path('requests/<int:pk>/act/', views.ApprovalActView.as_view(), name='approval-act'),

    # Admin configuration
    path('workflows/', views.WorkflowListCreateView.as_view(), name='approval-workflow-list'),
    path('workflows/<int:pk>/', views.WorkflowDetailView.as_view(),
         name='approval-workflow-detail'),
    path('workflows/<int:pk>/preview/', views.WorkflowPreviewView.as_view(),
         name='approval-workflow-preview'),
    path('levels/', views.LevelListCreateView.as_view(), name='approval-level-list'),
    path('levels/<int:pk>/', views.LevelDetailView.as_view(), name='approval-level-detail'),
    path('levels/<int:level_id>/approvers/', views.LevelApproverListCreateView.as_view(),
         name='approval-level-approver-list'),
    path('approvers/<int:pk>/', views.LevelApproverDetailView.as_view(),
         name='approval-level-approver-detail'),
]
