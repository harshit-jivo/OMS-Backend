"""Advance Payment routes. Mounted at ``api/advance-payments/``.

Every endpoint requires `Advance_Payment` and takes a mandatory `?company=`
(OIL | BEVERAGES | MART):

  GET /vendors/               ?company=&search=&limit=
  GET /customers/             ?company=&search=&limit=
  GET /employees/             ?company=&search=&limit=
  GET /open-purchase-orders/  ?company=&card_code=&search=&limit=
  GET /document-attachment/   ?company=&kind=po|bill&doc_entry=  (latest SAP attachment, the file)
  GET /document-attachment/read/  same params  (its invoice fields, checked against SAP)
  POST /payment-proof/        multipart: file, company, amount, to_account, card_code, invoices
  GET|POST /employee-master/  ?search=&role=&is_active=  (admins: the employee master)
  GET /employee-directory/     ?roles=1,2&search=&not_in_sap=1&company=  (request form pickers)
  GET /departments/            (departments with their sub-departments)
  GET /open-invoices/         ?company=&party_type=vendor|customer
                              &card_code=&search=&limit=
  GET /open-documents/        ?company=&card_code=&limit=
  GET /open-other-documents/  ?company=&card_code=&limit=   (vendor "All")
  GET /partner-bank-accounts/ ?company=&card_code=           (payee's banks)
  GET /house-banks/           ?company=                     (our banks, all)
  GET /cash-accounts/         ?company=                     (our cash G/Ls)

`/open-invoices/` lists unpaid invoices across a company, for picking one.
`/open-documents/` is ONE partner's whole open ledger — invoices, credit
memos, payments on account and journals — read from JDT1, which is what SAP's
Business Partner Ageing reads and the only place all of them appear together.

The requests and their approval (see `views.py`, `services/flow.py`):

  GET|POST /requests/                    ?scope=mine|desk ; raise (multipart data=JSON, files)
  GET  /requests/<id>/                   one, with its history and its route
  POST /requests/<id>/edit/              the creator's edit (+ remove_file_ids, resubmit)
  POST /requests/<id>/<action>/          approve | reject | return | send-back |
                                         cancel | resubmit     {remarks, version}
  PUT  /requests/<id>/payout/            the Payment stage's payment details
  POST /requests/<id>/confirm-password/  before typing a payee account by hand
  POST /requests/<id>/payout-lines/<l>/utr/   after completion: the bank's UTR
  POST /requests/<id>/files/             a bank / payment proof
  GET|DELETE /requests/<id>/files/<f>/
"""
from django.urls import path

from . import views

urlpatterns = [
    path('vendors/', views.VendorsView.as_view(),
         name='advance-payment-vendors'),
    path('customers/', views.CustomersView.as_view(),
         name='advance-payment-customers'),
    path('employees/', views.EmployeesView.as_view(),
         name='advance-payment-employees'),
    path('open-purchase-orders/', views.OpenPurchaseOrdersView.as_view(),
         name='advance-payment-open-purchase-orders'),
    path('document-attachment/', views.DocumentAttachmentView.as_view(),
         name='advance-payment-document-attachment'),
    path('document-attachment/read/', views.DocumentAttachmentReadView.as_view(),
         name='advance-payment-document-attachment-read'),
    path('payment-proof/', views.PaymentProofView.as_view(),
         name='advance-payment-payment-proof'),
    path('employee-master/', views.EmployeeMasterView.as_view(),
         name='advance-payment-employee-master'),
    path('employee-directory/', views.EmployeeDirectoryView.as_view(),
         name='advance-payment-employee-directory'),
    path('departments/', views.DepartmentsView.as_view(),
         name='advance-payment-departments'),
    path('open-invoices/', views.OpenInvoicesView.as_view(),
         name='advance-payment-open-invoices'),
    path('open-documents/', views.OpenDocumentsView.as_view(),
         name='advance-payment-open-documents'),
    path('open-other-documents/', views.OtherDocumentsView.as_view(),
         name='advance-payment-open-other-documents'),
    path('partner-bank-accounts/', views.PartnerBankAccountsView.as_view(),
         name='advance-payment-partner-bank-accounts'),
    path('house-banks/', views.HouseBanksView.as_view(),
         name='advance-payment-house-banks'),
    path('cash-accounts/', views.CashAccountsView.as_view(),
         name='advance-payment-cash-accounts'),

    path('requests/', views.RequestListView.as_view(),
         name='advance-payment-requests'),
    path('requests/<int:pk>/', views.RequestDetailView.as_view(),
         name='advance-payment-request'),
    path('requests/<int:pk>/edit/', views.RequestEditView.as_view(),
         name='advance-payment-request-edit'),
    path('requests/<int:pk>/payout/', views.RequestPayoutView.as_view(),
         name='advance-payment-request-payout'),
    path('requests/<int:pk>/confirm-password/', views.RequestConfirmPasswordView.as_view(),
         name='advance-payment-request-confirm-password'),
    path('requests/<int:pk>/payout-lines/<int:line_id>/utr/', views.RequestUtrView.as_view(),
         name='advance-payment-request-utr'),
    path('requests/<int:pk>/files/', views.RequestFilesView.as_view(),
         name='advance-payment-request-files'),
    path('requests/<int:pk>/files/<int:file_id>/', views.RequestFileView.as_view(),
         name='advance-payment-request-file'),
    path('requests/<int:pk>/<slug:action>/', views.RequestActionView.as_view(),
         name='advance-payment-request-action'),
]
