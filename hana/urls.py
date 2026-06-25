from django.urls import path
from .views import GetSalesOrderView , GetOpenPartiesView , GetCustomerDetailsView , GetWarehouseDetailsView , GetSalespersonDetailsView ,GetFreightMastersView , GetAddressView ,GetStateChainView , GetVendorStatesView  , GetAllCustomersView,GetNextDocNumberView , GetFGItemsView  , GetBatchDetailsView ,GetInventoryDetailsView , GetItemPriceView, GetProductStockView, GetProductSalesOrderView , GetSeries , GetDraftVerification , GetInvoiceDrafts

urlpatterns = [
    path('product-stock/' , GetProductStockView.as_view()),
    path('so/' , GetSalesOrderView.as_view()),
    path('product-so/' , GetProductSalesOrderView.as_view()),
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
    path('fg-items/' , GetFGItemsView.as_view()),
    path('batch-details/' , GetBatchDetailsView.as_view()),
    path('inventory-details/' , GetInventoryDetailsView.as_view()),
    path('item-price/' , GetItemPriceView.as_view()),
    path('series/' , GetSeries.as_view()),
    path('draft/verify' , GetDraftVerification.as_view()),
    path('invoice-drafts/', GetInvoiceDrafts.as_view())
]
