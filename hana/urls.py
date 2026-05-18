from django.urls import path
from .views import GetSalesOrderView

urlpatterns = [
    path('so/' , GetSalesOrderView.as_view()),
]