from django.urls import path

from .views import (
    AdminLabelDetailView, AdminLabelListCreateView, PublicFieldsView,
    PublicLabelsView,
)

urlpatterns = [
    # Public read map — fetched once after login by web + mobile.
    path('labels/', PublicLabelsView.as_view(), name='ui-config-labels'),
    # Public field-behaviour config (enabled/required flags per field key).
    path('fields/', PublicFieldsView.as_view(), name='ui-config-fields'),

    # Admin CRUD.
    path('admin/labels/', AdminLabelListCreateView.as_view(),
         name='ui-config-admin-labels'),
    path('admin/labels/<int:pk>/', AdminLabelDetailView.as_view(),
         name='ui-config-admin-label-detail'),
]
