from django.urls import path
from .views import SAPInvoiceCreateView , DraftCreateView , DraftActionView

urlpatterns = [
    path('invoice/' , SAPInvoiceCreateView.as_view()),
    path('draft/' , DraftCreateView.as_view()),
    path('draft-action/' , DraftActionView.as_view())

]