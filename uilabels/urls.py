from django.urls import path

from .views import (
    AdminLabelDetailView, AdminLabelListCreateView, PublicLabelsView,
)

urlpatterns = [
    # Public read map — fetched once after login by web + mobile.
    path('labels/', PublicLabelsView.as_view(), name='ui-config-labels'),

    # Admin CRUD.
    path('admin/labels/', AdminLabelListCreateView.as_view(),
         name='ui-config-admin-labels'),
    path('admin/labels/<int:pk>/', AdminLabelDetailView.as_view(),
         name='ui-config-admin-label-detail'),
]
