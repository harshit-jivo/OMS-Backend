from django.urls import path
from .views import SAPInvoiceCreateView , DraftView , DraftActionView 

urlpatterns = [
    path('invoice/' , SAPInvoiceCreateView.as_view()),
    path('draft/' , DraftView.as_view()),
    path('draft-action/' , DraftActionView.as_view())

]