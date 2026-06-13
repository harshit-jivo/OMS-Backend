from django.urls import path
from .views import SAPInvoiceCreateView , DraftCreateView

urlpatterns = [
    path('invoice/' , SAPInvoiceCreateView.as_view()),
    path('draft/' , DraftCreateView.as_view())
]