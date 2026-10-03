"""App configuration for the Control Panel.

The management dashboards — Realise, Sales, Inventory, Expenses, Salaries,
COGS and the Overview that summarises them — ported from the standalone
C_Panel Django app into OMS, so they sit behind OMS's login and OMS's page
permissions instead of a second user table and a set of Django groups.

Every figure is read from SAP HANA live (see `services/hana.py`). What the app
stores is its own: the sales targets the Realise screen is measured against,
and the expense budgets the Expenses screen compares actuals to.
"""
from django.apps import AppConfig


class ControlPanelConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'control_panel'
    verbose_name = 'Control Panel'
