from django.urls import path
from .views import SAPInvoiceCreateView , DraftView , DraftActionView
from .ap_views import (
    OpenGRPOListView,
    GRPODetailView,
    VendorTDSView,
    APAttachmentUploadView,
    APInvoiceCreateView,
)

urlpatterns = [
    path('invoice/' , SAPInvoiceCreateView.as_view()),
    path('draft/' , DraftView.as_view()),
    path('draft-action/' , DraftActionView.as_view()),

    # AP (accounts payable) invoice data entry
    path('ap/open-grpos/', OpenGRPOListView.as_view()),
    path('ap/grpo/', GRPODetailView.as_view()),
    path('ap/vendor-tds/', VendorTDSView.as_view()),
    path('ap/attachment/', APAttachmentUploadView.as_view()),
    path('ap/invoice/', APInvoiceCreateView.as_view()),
]