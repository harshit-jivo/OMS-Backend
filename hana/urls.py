from django.urls import path
from .views import getSalesOrderView

urlpatterns = [
    path('so/' , getSalesOrderView.as_view()),
]