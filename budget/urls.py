from django.urls import path

from budget import views

urlpatterns = [
    path('queue/', views.QueueView.as_view(), name='budget-queue'),
    path('history/', views.HistoryView.as_view(), name='budget-history'),
    path('drafts/', views.DraftListView.as_view(), name='budget-drafts'),
    path('drafts/<int:pk>/', views.DraftDetailView.as_view(), name='budget-draft'),
    path('items/<int:pk>/approve/', views.ApproveView.as_view(), name='budget-approve'),
    path('items/<int:pk>/reject/', views.RejectView.as_view(), name='budget-reject'),
    path('items/<int:pk>/retry-sap/', views.RetrySapView.as_view(), name='budget-retry-sap'),
    path('settings/', views.SettingsView.as_view(), name='budget-settings'),
    path('health/', views.HealthView.as_view(), name='budget-health'),
    path('users/', views.UsersView.as_view(), name='budget-users'),
]
