"""App configuration for Advance Payment.

NO MODELS, AND THAT IS DELIBERATE — FOR NOW.

Every endpoint in this app reads SAP live: open purchase orders, open
invoices, vendors, customers and the employee-advance GL accounts all exist in
the company databases and nowhere else. Copying them into Postgres would give
OMS a second, staler answer to a question SAP already answers, and the module
this one sits next to (`production`) exists because exactly that went wrong —
a sync reported success for 33 days while writing nothing.

The app will grow a model when it has something of its OWN to store: the
advance request, who asked, who approved, what SAP document it produced.
Those are OMS facts. The lists it picks from are not.

There is therefore no migration, and nothing to register with the Workflow
Engine yet.
"""
from django.apps import AppConfig


class AdvancePaymentConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'advance_payment'
    verbose_name = 'Advance Payment'
