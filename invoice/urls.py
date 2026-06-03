from django.urls import path
from .views import SAPInvoiceCreateView

urlpatterns = [
    path('invoice/' , SAPInvoiceCreateView.as_view())
]