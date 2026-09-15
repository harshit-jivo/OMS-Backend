"""Workflow engine routes. Mounted at ``api/workflow/`` (and ``api/v1/workflow/``).

CONFIGURATION ONLY. Every route requires `workflow.config.manage`.

  GET  POST        /api/workflow/modules/
  GET  PATCH PUT   /api/workflow/modules/<pk>/
  GET  POST        /api/workflow/workflows/            ?module=CODE&company=OIL
  GET  PATCH PUT   /api/workflow/workflows/<pk>/
  GET  POST        /api/workflow/queries/              ?workflow=<id>
  GET  PATCH PUT   /api/workflow/queries/<pk>/
  POST             /api/workflow/queries/<pk>/revalidate/
  GET  POST        /api/workflow/stages/               ?workflow=<id>
                                                      ?user=<id>
  GET  PATCH PUT   /api/workflow/stages/<pk>/
  GET  POST        /api/workflow/replacements/
  GET  PATCH PUT   /api/workflow/replacements/<pk>/

There is no DELETE on any of them — every row is referenced by a module's
runtime or its history, so retiring one is `is_active = false`.

WHAT IS NOT HERE, AND WHY
-------------------------
No `/inbox/`, `/tasks/`, `/tasks/<id>/approve/`, `/tasks/<id>/reject/`,
`/flows/.../state/` and no `/testflow/`. Approval runtime belongs to the
business module: it owns its task, its action history and its lifecycle, and
publishes its own endpoints for them. This engine answers one question, in
process rather than over HTTP:

    workflow.services.selection.select_for_module(
        module_code='BUDGET', document_id=123, company='OIL')

which returns the workflow, the query that matched, and the ordered stages
with their configured and effective users.
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
]
