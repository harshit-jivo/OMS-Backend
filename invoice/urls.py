from django.urls import path
from .views import InvoiceLogCreateView , InvoiceLogListView , InvoicelogStatusUpdateView ,InvoiceHistoryView , InvoiceRefLogCreateView ,UpdateInvoiceLogView , CreditLimitCardsView , CreditLimitRequestView ,GetCreditLimitJSAPFlow ,GetPrintReport

urlpatterns = [
    path('pending/', InvoiceLogCreateView.as_view(), name='invoice-log-pending'),
    path('all/', InvoiceLogListView.as_view(), name='invoice-log-pending'),
    path('refLogs/' , InvoiceRefLogCreateView.as_view() ),
    path('history/<int:pk>/', InvoiceHistoryView.as_view(), name='invoice-history'),
    path('<int:pk>/update-status/', InvoicelogStatusUpdateView.as_view(), name='invoice-log-update-status'),
    path('log/<int:id>/' , UpdateInvoiceLogView.as_view()),
    path('credit-limit/cards/', CreditLimitCardsView.as_view(), name='credit-limit-cards'),
    path('credit-limit/request/', CreditLimitRequestView.as_view(), name='credit-limit-request'),
    path('credit-limit/flow/' , GetCreditLimitJSAPFlow.as_view()),
    path('crystal/' , GetPrintReport.as_view())

]