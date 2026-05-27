from django.urls import path
from .views import GetSalesOrderView , GetOpenPartiesView , GetCustomerDetailsView , GetWarehouseDetailsView , GetSalespersonDetailsView ,GetFreightMastersView , GetAddressView ,GetStateChainView , GetVendorStatesView  , GetAllCustomersView,GetNextDocNumberView
urlpatterns = [
    path('so/' , GetSalesOrderView.as_view()),
    path('open-parties/' , GetOpenPartiesView.as_view()),
    path('customer-details/' , GetCustomerDetailsView.as_view()),
    path('warehouse-details/' , GetWarehouseDetailsView.as_view()),
    path('salesperson-details/' , GetSalespersonDetailsView.as_view()),
    path('freight-masters/' , GetFreightMastersView.as_view()),
    path('address/' , GetAddressView.as_view()),
    path('state-chain/' , GetStateChainView.as_view()),
    path('vendor-states/' , GetVendorStatesView.as_view()),
    path('all-customers/' , GetAllCustomersView.as_view()),
    path('next-doc-number/' , GetNextDocNumberView.as_view()),
]