from django.urls import path
from .views import (
    SyncAllView, SyncProductsView, SyncPartiesView, SyncPartyAddressesView,
    SyncBranchesView, ProductListView, ProductDetailView, ProductByCodeView,
    PartyListView, PartyDetailView, PartyByCodeView, PartyAddressListView,
    BranchListView, SyncLogListView, SyncScheduleListView,
    SyncScheduleDetailView, ToggleScheduleView, SyncStatusView,
    PushSalesQuotationView, TestSalesQuotation, ApproveOrderAPIView, SalesQuotationLogByOrderView,
)
 
urlpatterns = [
    
    # ============ Sync Operations (Manual Trigger) ============
    path('sync/all/', SyncAllView.as_view(), name='sync-all'),
    path('sync/products/', SyncProductsView.as_view(), name='sync-products'),
    path('sync/parties/', SyncPartiesView.as_view(), name='sync-parties'),
    path('sync/addresses/', SyncPartyAddressesView.as_view(), name='sync-addresses'),
    path('sync/branches/', SyncBranchesView.as_view(), name='sync-branches'),  # ✅ POST
    
    # ============ Products ============
    path('products/', ProductListView.as_view(), name='product-list'),
    path('products/<int:pk>/', ProductDetailView.as_view(), name='product-detail'),
    path('products/code/<str:item_code>/', ProductByCodeView.as_view(), name='product-by-code'),
    
    # ============ Parties ============
    path('parties/', PartyListView.as_view(), name='party-list'),
    path('parties/<int:pk>/', PartyDetailView.as_view(), name='party-detail'),
    path('parties/code/<str:card_code>/', PartyByCodeView.as_view(), name='party-by-code'),
    
    # ============ Branches ============
    path('branches/', BranchListView.as_view(), name='branch-list'),  # ✅ GET
    
    # ============ Party Addresses ============
    path('addresses/', PartyAddressListView.as_view(), name='address-list'),
    
    # ============ Sync Logs ============
    path('logs/', SyncLogListView.as_view(), name='sync-logs'),
    path('quotation-log/<int:order_id>/', SalesQuotationLogByOrderView.as_view(), name='quotation-log-by-order'),
    
    # ============ Schedule Management ============
    path('schedules/', SyncScheduleListView.as_view(), name='schedule-list'),
    path('schedules/<int:pk>/', SyncScheduleDetailView.as_view(), name='schedule-detail'),
    path('schedules/<int:pk>/toggle/', ToggleScheduleView.as_view(), name='schedule-toggle'),
    
    # ============ Status ============
    path('status/', SyncStatusView.as_view(), name='sync-status'),
    path('push-quotation/', PushSalesQuotationView.as_view(), name='push-quotation'),

    path('test-quotation/', TestSalesQuotation.as_view()),

    path('test-quotation/<int:pk>/', TestSalesQuotation.as_view()),
    path("approve-order/", ApproveOrderAPIView.as_view(), name="approve-order"),
    
    
    
]
