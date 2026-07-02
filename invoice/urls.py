from django.urls import path
from .views import InvoiceLogCreateView , InvoiceLogListView  , InvoiceRefLogCreateView

urlpatterns = [
    path('log/create/', InvoiceLogCreateView.as_view(), name='invoice-log-pending'),
    path('all/', InvoiceLogListView.as_view(), name='invoice-log-pending'),
    path('refLogs/' , InvoiceRefLogCreateView.as_view() ),

]