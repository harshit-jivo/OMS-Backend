from django.urls import path
from .views import InvoiceLogCreateView , InvoiceLogListView , InvoicelogStatusUpdateView ,InvoiceHistoryView

urlpatterns = [
    path('pending/', InvoiceLogCreateView.as_view(), name='invoice-log-pending'),
    path('all/', InvoiceLogListView.as_view(), name='invoice-log-pending'),
    path('history/<int:pk>/', InvoiceHistoryView.as_view(), name='invoice-history'),
    path('<int:pk>/update-status/', InvoicelogStatusUpdateView.as_view(), name='invoice-log-update-status'),
    

]