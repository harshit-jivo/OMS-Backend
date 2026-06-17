from django.urls import path
from .views import SAPInvoiceCreateView , DraftCreateView , DraftApproveView

urlpatterns = [
    path('invoice/' , SAPInvoiceCreateView.as_view()),
    path('draft/' , DraftCreateView.as_view()),
    path('approve-draft/' , DraftApproveView.as_view())

]