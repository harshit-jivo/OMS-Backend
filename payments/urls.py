from django.urls import path

from attachments.views import AttachmentDownloadView

from . import views

urlpatterns = [
    # What the caller may do — drives button visibility in the clients.
    path('my-permissions/', views.MyPaymentPermissionsView.as_view(),
         name='payment-my-permissions'),

    # Admin CRUD for master data — all manageable from the web UI so nothing
    # here requires Django admin.
    path('company-mappings/', views.CompanyMappingListCreateView.as_view(),
         name='payment-company-mapping-list'),
    path('company-mappings/<int:pk>/', views.CompanyMappingDetailView.as_view(),
         name='payment-company-mapping-detail'),
    path('admin/bank-accounts/', views.BankAccountAdminListCreateView.as_view(),
         name='payment-admin-bank-account-list'),
    path('admin/bank-accounts/<int:pk>/', views.BankAccountAdminDetailView.as_view(),
         name='payment-admin-bank-account-detail'),
    path('admin/collection-persons/',
         views.CollectionPersonAdminListCreateView.as_view(),
         name='payment-admin-collection-person-list'),
    path('admin/collection-persons/<int:pk>/',
         views.CollectionPersonAdminDetailView.as_view(),
         name='payment-admin-collection-person-detail'),

    # Cascade: company -> parties -> open invoices
    path('companies/', views.CompanyListView.as_view(), name='payment-companies'),
    path('parties/', views.PartyListView.as_view(), name='payment-parties'),
    path('open-invoices/', views.OpenInvoiceListView.as_view(),
         name='payment-open-invoices'),
    path('collection-persons/', views.CollectionPersonListView.as_view(),
         name='payment-collection-persons'),
    path('bank-accounts/', views.BankAccountListView.as_view(),
         name='payment-bank-accounts'),

    # Receipts
    path('receipts/', views.PaymentReceiptListCreateView.as_view(),
         name='payment-receipt-list'),
    path('receipts/<int:pk>/', views.PaymentReceiptDetailView.as_view(),
         name='payment-receipt-detail'),
    path('receipts/<int:pk>/submit/', views.PaymentReceiptSubmitView.as_view(),
         name='payment-receipt-submit'),
    path('receipts/<int:pk>/history/', views.PaymentReceiptHistoryView.as_view(),
         name='payment-receipt-history'),
    path('receipts/<int:pk>/sap-history/',
         views.PaymentReceiptSapHistoryView.as_view(),
         name='payment-receipt-sap-history'),
    path('receipts/<int:pk>/attachments/',
         views.ReceiptAttachmentUploadView.as_view(),
         name='payment-receipt-attachment-upload'),

    # Deposits
    path('deposits/', views.BankDepositListCreateView.as_view(),
         name='bank-deposit-list'),
    path('deposits/<int:pk>/', views.BankDepositDetailView.as_view(),
         name='bank-deposit-detail'),
    path('deposits/<int:pk>/submit/', views.BankDepositSubmitView.as_view(),
         name='bank-deposit-submit'),
    path('deposits/<int:pk>/attachments/',
         views.DepositAttachmentUploadView.as_view(),
         name='bank-deposit-attachment-upload'),
    path('depositable-receipts/', views.DepositableReceiptListView.as_view(),
         name='payment-depositable-receipts'),

    # Attachment download — permission-checked, never a raw share path.
    path('attachments/<int:pk>/download/', AttachmentDownloadView.as_view(),
         name='payment-attachment-download'),
]
