"""App configuration for Advance Payment.

The lists an advance is raised against (open purchase orders, open invoices,
vendors, customers, the employee-advance GL accounts) are read from SAP live
and are NOT copied into Postgres: SAP already answers them, and a copy would
be a second, staler answer.

What the app stores is its own: `Employee` (models.py), the employee master
with each person's role (HOD / Sub-HOD / Executive), first loaded from JSAP by
migration 0002. The advance request itself will be the next model.
"""
from django.apps import AppConfig


class AdvancePaymentConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'advance_payment'
    verbose_name = 'Advance Payment'
