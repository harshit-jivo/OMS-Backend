from django.urls import path
from .views import GetSalesOrderView , GetOpenPartiesView , GetCustomerDetailsView , GetWarehouseDetailsView , GetSalespersonDetailsView ,GetFreightMastersView

urlpatterns = [
    path('so/' , GetSalesOrderView.as_view()),
    path('open-parties/' , GetOpenPartiesView.as_view()),
    path('customer-details/' , GetCustomerDetailsView.as_view()),
    path('warehouse-details/' , GetWarehouseDetailsView.as_view()),
    path('salesperson-details/' , GetSalespersonDetailsView.as_view()),
    path('freight-masters/' , GetFreightMastersView.as_view())
]