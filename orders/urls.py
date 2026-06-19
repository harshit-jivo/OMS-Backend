from django.contrib import admin
from django.urls import path,include
from .views import DashboardKPIView,WDashboardKPIView,WDashboardChartsView,OrderStatusTrackingView,OrderLogsByOrderView, OrderDetailsByOrderView,OrdersByUserView,DashboardChartsView,OrderStatusList,BranchView,PartyView,DispatchLocationListView, SchemeProductView,UpdateOrderStatusView,PartyAddressesView,ProductFiltersView,ProductListView,PartyProductsView,CreateOrderView,UpdateOrderView,OrderListView,RejectOrderView,ApproveOrderView,SchemeListView,CreateSchemeView, ai_order_summary, TemplatePartyListView, TemplateOrderListView, NotificationListView, PushTokenView,StaffProductsAPIView, OrderStockCheckView, OrderFlowConfigView, PartyOrderFlowConfigView

urlpatterns=[
    
    # path('scheme-products/', SchemeProductView.as_view(), name='scheme-products'),
    path('parties/',PartyView.as_view(),name='parties'),
    path('dispatches/',DispatchLocationListView.as_view(),name='dispatches'),
    path('addresses/', PartyAddressesView.as_view(), name='party-addresses'),
    path('product-filters/', ProductFiltersView.as_view(), name='product-filters'),
    path('products/', ProductListView.as_view(), name='products'),
    path('create/', CreateOrderView.as_view(), name='create-order'),
    path('<int:order_id>/update/', UpdateOrderView.as_view(), name='update-order'),
    path('list/', OrderListView.as_view(), name='order_list'),
    path('<int:order_id>/approve/', ApproveOrderView.as_view(), name='approve_list'),
    path('<int:order_id>/reject/', RejectOrderView.as_view(), name='reject_list'),
    path('party-products/<str:card_code>/', PartyProductsView.as_view(), name='party-products'),
    path('schemes/', SchemeListView.as_view(), name='schemes'),
    path('status/',OrderStatusList.as_view(),name='status'),
    path('flow-config/',OrderFlowConfigView.as_view(),name='order-flow-config'),
    path('party-flow-config/',PartyOrderFlowConfigView.as_view(),name='party-order-flow-config'),
    path('branch/',BranchView.as_view(),name='branch'),
    path('<int:order_id>/update-status/',UpdateOrderStatusView.as_view(),name='update-status'),
    path('dashboard/', DashboardKPIView.as_view(), name='dashboard'),
    path('dashboard/charts/',DashboardChartsView.as_view(), name='dashboard-charts'),
    path('dashboardW/', WDashboardKPIView.as_view(), name='Wdashboard'),
    path('dashboardW/charts/',WDashboardChartsView.as_view(), name='Wdashboard-charts'),
    path('status-tracking/',OrderStatusTrackingView.as_view(), name='status-tracking'),
    path("<int:order_id>/orderlogs/",OrderLogsByOrderView.as_view(),name="order-logs-by-orderid"),
    path("orderdetailsbyid/<int:order_id>/",OrderDetailsByOrderView.as_view(),name="order-details-by-id"),
    path("<int:order_id>/orderdetails/",OrderDetailsByOrderView.as_view(),name="order-details-by-orderid"),
    path("ordersbyuser/<int:user_id>/",OrdersByUserView.as_view(),name="orders-by-user"),
    path("create-scheme/",CreateSchemeView.as_view(), name="create-scheme"),
    path("api/ai-order-summary/", ai_order_summary),
    path('templates/parties/', TemplatePartyListView.as_view(), name='template_parties'),
    path('templates/orders/', TemplateOrderListView.as_view(), name='template_orders'),
    path("stock-check/",OrderStockCheckView.as_view(), name="stock-check"),
    path("notifications/",NotificationListView.as_view(), name="notifications"),
    path("notifications/<int:pk>/",NotificationListView.as_view(), name="notification-detail"),
    path("push-token/",PushTokenView.as_view(), name="push-token"),
    path(
    "staff-products/",StaffProductsAPIView.as_view(),name="staff-products")
    
    
    
]
