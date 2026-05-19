from django.urls import path
from .views import GetSalesOrderView , GetOpenPartiesView

urlpatterns = [
    path('so/' , GetSalesOrderView.as_view()),
    path('open-parties/' , GetOpenPartiesView.as_view()),
]