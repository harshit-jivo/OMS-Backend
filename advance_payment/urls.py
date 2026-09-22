"""Advance Payment routes. Mounted at ``api/advance-payments/``.

Every endpoint requires `Advance_Payment` and takes a mandatory `?company=`
(OIL | BEVERAGES | MART):

  GET /vendors/               ?company=&search=&limit=
  GET /customers/             ?company=&search=&limit=
  GET /employees/             ?company=&search=&limit=
  GET /open-purchase-orders/  ?company=&card_code=&search=&limit=
  GET /open-invoices/         ?company=&party_type=vendor|customer
                              &card_code=&search=&limit=

THERE IS NO POST YET. This is the picking half of the module: what an advance
can be raised against. The request itself, its approval and the SAP down
payment it produces are the next piece, and they will need models — these
lookups do not, because every row they return already belongs to SAP.
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
    path('open-invoices/', views.OpenInvoicesView.as_view(),
         name='advance-payment-open-invoices'),
]
