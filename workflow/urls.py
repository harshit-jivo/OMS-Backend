"""Workflow engine routes. Mounted at ``api/workflow/`` (and ``api/v1/workflow/``).

Configuration (requires `workflow.config.manage`):
  GET  POST         /api/workflow/modules/
  GET  PATCH DELETE /api/workflow/modules/<pk>/
  GET  POST         /api/workflow/workflows/            ?module=TESTFLOW
  GET  PATCH DELETE /api/workflow/workflows/<pk>/
  GET  POST         /api/workflow/queries/              ?workflow=<id>
  GET  PATCH DELETE /api/workflow/queries/<pk>/
  POST              /api/workflow/queries/<pk>/revalidate/
  GET  POST         /api/workflow/stages/               ?workflow=<id>
  GET  PATCH DELETE /api/workflow/stages/<pk>/
  GET  POST         /api/workflow/replacements/
  GET  PATCH DELETE /api/workflow/replacements/<pk>/

Runtime:
  GET               /api/workflow/inbox/                     (authenticated)
  GET               /api/workflow/tasks/<pk>/                (authenticated)
  POST              /api/workflow/tasks/<pk>/approve/        (workflow.task.act)
  POST              /api/workflow/tasks/<pk>/reject/         (workflow.task.act)
  GET               /api/workflow/flows/<module_code>/<flow_id>/state/
                                                             (workflow.state.view)

TestFlow harness — the first integration target:
  GET  POST         /api/workflow/testflow/documents/
  GET               /api/workflow/testflow/documents/<pk>/
  POST              /api/workflow/testflow/documents/<pk>/submit/
"""
from django.urls import path

from . import views

urlpatterns = [
    # --- configuration ---------------------------------------------------
    path('modules/', views.ModuleListCreateView.as_view(),
         name='workflow-module-list'),
    path('modules/<int:pk>/', views.ModuleDetailView.as_view(),
         name='workflow-module-detail'),

    path('workflows/', views.WorkflowListCreateView.as_view(),
         name='workflow-list'),
    path('workflows/<int:pk>/', views.WorkflowDetailView.as_view(),
         name='workflow-detail'),

    path('queries/', views.QueryListCreateView.as_view(),
         name='workflow-query-list'),
    path('queries/<int:pk>/', views.QueryDetailView.as_view(),
         name='workflow-query-detail'),
    path('queries/<int:pk>/revalidate/', views.QueryRevalidateView.as_view(),
         name='workflow-query-revalidate'),

    path('stages/', views.StageListCreateView.as_view(),
         name='workflow-stage-list'),
    path('stages/<int:pk>/', views.StageDetailView.as_view(),
         name='workflow-stage-detail'),

    path('replacements/', views.ReplacementListCreateView.as_view(),
         name='workflow-replacement-list'),
    path('replacements/<int:pk>/', views.ReplacementDetailView.as_view(),
         name='workflow-replacement-detail'),

    # --- runtime ---------------------------------------------------------
    path('inbox/', views.InboxView.as_view(), name='workflow-inbox'),
    path('tasks/<int:pk>/', views.TaskDetailView.as_view(),
         name='workflow-task-detail'),
    path('tasks/<int:pk>/approve/', views.TaskApproveView.as_view(),
         name='workflow-task-approve'),
    path('tasks/<int:pk>/reject/', views.TaskRejectView.as_view(),
         name='workflow-task-reject'),
    path('flows/<str:module_code>/<int:flow_id>/state/',
         views.FlowStateView.as_view(), name='workflow-flow-state'),

    # --- TestFlow harness ------------------------------------------------
    path('testflow/documents/', views.TestDocumentListCreateView.as_view(),
         name='workflow-testdoc-list'),
    path('testflow/documents/<int:pk>/',
         views.TestDocumentDetailView.as_view(), name='workflow-testdoc-detail'),
    path('testflow/documents/<int:pk>/submit/',
         views.TestDocumentSubmitView.as_view(), name='workflow-testdoc-submit'),
]
