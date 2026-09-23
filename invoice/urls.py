from django.urls import path
from .views import InvoiceLogCreateView , InvoiceLogListView , InvoicelogStatusUpdateView ,InvoiceHistoryView , InvoiceRefLogCreateView ,UpdateInvoiceLogView , CreditLimitCardsView , CreditLimitRequestView ,GetCreditLimitJSAPFlow ,GetPrintReport , InvoiceLogListwoWhsView , InvoiceLogDeleteView , InvoicePostToSapView , UsedSalesOrdersView , ReservedBatchesView

urlpatterns = [
    path('pending/', InvoiceLogCreateView.as_view(), name='invoice-log-pending'),
    path('all/', InvoiceLogListView.as_view(), name='invoice-log-pending'),
    path('refLogs/' , InvoiceRefLogCreateView.as_view() ),
    path('history/<int:pk>/', InvoiceHistoryView.as_view(), name='invoice-history'),
    path('<int:pk>/update-status/', InvoicelogStatusUpdateView.as_view(), name='invoice-log-update-status'),
    # The one way an invoice reaches SAP: posts the stored payload, records the outcome.
    path('<int:pk>/post-to-sap/', InvoicePostToSapView.as_view(), name='invoice-log-post-to-sap'),
    # DELETE removes the entry from the review screen; POST restores it.
    path('<int:pk>/delete/', InvoiceLogDeleteView.as_view(), name='invoice-log-delete'),
    path('log/<int:id>/' , UpdateInvoiceLogView.as_view()),
    path('credit-limit/cards/', CreditLimitCardsView.as_view(), name='credit-limit-cards'),
    path('credit-limit/request/', CreditLimitRequestView.as_view(), name='credit-limit-request'),
    path('credit-limit/flow/' , GetCreditLimitJSAPFlow.as_view()),
    path('crystal/' , GetPrintReport.as_view()),
    path('logs/all/' , InvoiceLogListwoWhsView.as_view()),
    # SOs already carried by an in-flight invoice log, for the SO picker.
    path('used-sales-orders/', UsedSalesOrdersView.as_view(), name='invoice-used-sales-orders'),
    # Batches an in-flight log already holds, so auto-allocation can skip them.
    path('reserved-batches/', ReservedBatchesView.as_view(), name='invoice-reserved-batches'),

]