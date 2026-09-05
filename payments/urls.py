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
    path('admin/collection-persons/',
         views.CollectionPersonAdminListCreateView.as_view(),
         name='payment-admin-collection-person-list'),
    path('admin/collection-persons/<int:pk>/',
         views.CollectionPersonAdminDetailView.as_view(),
         name='payment-admin-collection-person-detail'),

    # Analytics behind the Payments Dashboard. The first call fills the whole
    # page; the other two exist so paging the table or opening one person does
    # not re-run the chart aggregations. All three require Payments_Dashboard.
    path('dashboard/', views.PaymentDashboardView.as_view(),
         name='payment-dashboard'),
    path('dashboard/collection-performance/',
         views.CollectionPerformanceView.as_view(),
         name='payment-dashboard-collection-performance'),
    # `kind` is in the path because a CollectionPerson and a User can share an
    # id and be different people.
    path('dashboard/person/<str:kind>/<int:pk>/',
         views.PersonAnalyticsView.as_view(),
         name='payment-dashboard-person'),

    # Cascade: company -> parties -> open invoices
    path('companies/', views.CompanyListView.as_view(), name='payment-companies'),
    path('parties/', views.PartyListView.as_view(), name='payment-parties'),
    path('open-invoices/', views.OpenInvoiceListView.as_view(),
         name='payment-open-invoices'),
    path('collection-persons/', views.CollectionPersonListView.as_view(),
         name='payment-collection-persons'),
    path('admin/method-mappings/',
         views.PaymentMethodMappingAdminView.as_view(),
         name='payment-method-mappings'),
    path('admin/method-mappings/<int:pk>/',
         views.PaymentMethodMappingAdminDetailView.as_view(),
         name='payment-method-mapping-detail'),
    path('admin/method-mapping-status/',
         views.PaymentMethodMappingStatusView.as_view(),
         name='payment-method-mapping-status'),
    path('sap-branches/', views.SapBranchListView.as_view(),
         name='payment-sap-branches'),
    path('banks/', views.BankAccountListView.as_view(),
         name='payment-banks'),
    path('bank-accounts/', views.BankAccountListView.as_view(),
         name='payment-bank-accounts'),

    # Receipts
    path('receipts/', views.PaymentReceiptListCreateView.as_view(),
         name='payment-receipt-list'),
    path('receipts/<int:pk>/', views.PaymentReceiptDetailView.as_view(),
         name='payment-receipt-detail'),
    path('receipts/<int:pk>/verify/', views.PaymentReceiptVerifyView.as_view(),
         name='payment-receipt-verify'),
    path('receipts/<int:pk>/submit/', views.PaymentReceiptSubmitView.as_view(),
         name='payment-receipt-submit'),
    path('receipts/<int:pk>/history/', views.PaymentReceiptHistoryView.as_view(),
         name='payment-receipt-history'),
    # OMS-generated SAP-style receipt PDF (posted receipts only). NOT the SAP
    # Crystal Report — see docs/SAP_CRYSTAL_RECEIPT_INTEGRATION.md.
    path('receipts/<int:pk>/sap-report/', views.SapReceiptPdfView.as_view(),
         name='payment-receipt-sap-report'),
    path('receipts/<int:pk>/attachments/',
         views.ReceiptAttachmentUploadView.as_view(),
         name='payment-receipt-attachment-upload'),

    # Deposits
    path('deposits/', views.BankDepositListCreateView.as_view(),
         name='bank-deposit-list'),
    path('deposits/<int:pk>/', views.BankDepositDetailView.as_view(),
         name='bank-deposit-detail'),
    path('deposits/<int:pk>/history/', views.BankDepositHistoryView.as_view(),
         name='bank-deposit-history'),
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
