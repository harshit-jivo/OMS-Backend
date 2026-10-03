from django.urls import path
from . import views

urlpatterns = [
    # Kept under the name 'home' so existing links and LOGIN_REDIRECT_URL still resolve;
    # the view itself just forwards to Sales. See home.views.landing.
    path('', views.landing, name='home'),
    path('users/', views.user_management, name='user_management'),
    path('api/users/save/', views.api_user_save, name='api_user_save'),
    path('api/users/delete/', views.api_user_delete, name='api_user_delete'),
]
