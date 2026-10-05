"""Who may open which Control Panel page.

The Control Panel is four pages, as the OMS sidebar shows it, each one a
single permission that opens the page with ALL of its sub-pages:

    Oils Sale   Overview · Map · Realise
    Sales       Sales Channel · Beverages Sale · Realise Dashboard · Sales · Targets
    Inventory   Dashboard · Stock & Warehouses · Stock Movement · Moving / Non-Moving ·
                FG Not Billed · ABC-XYZ Analysis · Aging Analysis · Item Trace ·
                Inventory Planning   (rupee values shown)
    Finance     Expenses · Salaries

plus C_Panel's report pages, one permission per report (REPORTS below), in
the groups C_Panel's own sidebar used: Sales Reports, Accounts, Inventory &
Production, Master Data. A report key opens that report only.

There is no segment limit: every holder sees all segments and may edit
targets (C_Panel's realise_admin). cpanel/core/oms_access.py turns these keys
into the C_Panel groups its code checks; cpanel/core/page_guard.py refuses a
page path to a user without its key.
"""
from core.permissions import HasAnyKey, effective_keys

OILS_SALE = 'control_panel.oils_sale'
SALES = 'control_panel.sales'
INVENTORY = 'control_panel.inventory'
FINANCE = 'control_panel.finance'

#: key -> (label, its sub-pages) — the four pages as Page Permissions shows them.
TREE = {
    OILS_SALE: ('Oils Sale', ('Overview', 'Map', 'Realise')),
    SALES: ('Sales', ('Sales Channel', 'Beverages Sale', 'Realise Dashboard', 'Sales', 'Targets')),
    INVENTORY: ('Inventory', ('Dashboard', 'Stock & Warehouses', 'Stock Movement', 'Moving / Non-Moving',
                              'FG Not Billed', 'ABC-XYZ Analysis', 'Aging Analysis', 'Item Trace',
                              'Inventory Planning')),
    FINANCE: ('Finance', ('Expenses', 'Salaries')),
}

#: C_Panel's report pages, one permission each:
#: (key suffix, label, sidebar group, OMS page id, path on this backend, the
#: C_Panel flag its views check). Rate List and Plan vs Done share the Realise
#: Calculator's flag, Beverages GST shares Customer Aging's - as in C_Panel -
#: so the page guard (PATH_KEYS) is what keeps those pages apart.
REPORTS = (
    ('compare_sales', 'Compare Sales', 'Sales Reports', 'compare-sales', '/realise/compare-sales/', 'can_compare_sales'),
    ('sales_cn', 'Sales vs Credit Notes', 'Sales Reports', 'sales-cn', '/realise/sales-cn/', 'can_sales_cn'),
    ('hidden_sales', 'Hidden Customer Sales', 'Sales Reports', 'hidden-sales', '/realise/hidden-sales/', 'can_hidden_sales'),
    ('sales_flow', 'Sales Document Flow', 'Sales Reports', 'sales-flow', '/realise/sales-flow/', 'can_sales_flow'),
    ('dispatch_details', 'Dispatch Details', 'Sales Reports', 'dispatch-details', '/realise/dispatch-details/', 'can_dispatch_details'),
    ('realise_calculator', 'Realise Calculator', 'Sales Reports', 'realise-calculator', '/realise/realise-calculator/', 'can_realise_calculator'),
    ('rate_list', 'Rate List', 'Sales Reports', 'rate-list', '/realise/rate-list/', 'can_realise_calculator'),
    ('plan_vs_done', 'Plan vs Done', 'Sales Reports', 'plan-vs-done', '/realise/plan-vs-done/', 'can_realise_calculator'),
    ('customer_aging', 'Customer Aging', 'Accounts', 'customer-aging', '/realise/customer-aging/', 'can_customer_aging'),
    ('beverages_gst', 'Beverages GST Data', 'Accounts', 'beverages-gst', '/realise/beverages-gst/', 'can_customer_aging'),
    ('required_credit_limit', 'Required Credit Limit', 'Accounts', 'required-credit-limit', '/realise/required-credit-limit/', 'can_required_credit_limit'),
    ('open_payments', 'Open Payments', 'Accounts', 'open-payments', '/realise/open-payments/', 'can_open_payments'),
    ('claims', 'Claims', 'Accounts', 'claims', '/realise/claims/', 'can_claims'),
    ('reconciliation', 'Wellness-Mart Reconciliation', 'Accounts', 'reconciliation', '/inventory/reconciliation/', 'can_reconciliation'),
    ('stock_available', 'Stock Available', 'Inventory & Production', 'stock-available', '/inventory/stock-available/', 'can_stock_available'),
    ('non_inventory', 'FG Non-Moving Stock', 'Inventory & Production', 'non-inventory', '/inventory/non-inventory/', 'can_non_inventory'),
    ('oih_vs_stock', 'OIH vs Stock', 'Inventory & Production', 'oih-vs-stock', '/realise/oih-vs-stock/', 'can_oih_vs_stock'),
    ('production', 'Production Plan', 'Inventory & Production', 'production', '/inventory/production/', 'can_production'),
    ('daily_production', 'Daily Production', 'Inventory & Production', 'daily-production', '/inventory/daily-production/', 'can_daily_production'),
    ('customer_master', 'Customer Master', 'Master Data', 'customer-master', '/realise/customer-master/', 'can_customer_master'),
)
REPORT_KEY = {r[0]: 'control_panel.report.' + r[0] for r in REPORTS}
#: report key -> the C_Panel flag it turns on.
REPORT_FLAGS = {REPORT_KEY[r[0]]: r[5] for r in REPORTS}

#: Oils Sale's inner tabs and Inventory's sections (the pages' own ids / OMS `?tab=`).
OILS_TABS = ('overview', 'map', 'realise')
INVENTORY_TABS = ('dash', 'stock', 'move', 'movers', 'billing', 'abc', 'aging', 'trace', 'planning')

#: Every Control Panel key: the four pages, then the reports.
ALL_CP_KEYS = (*TREE, *REPORT_KEY.values())

#: OMS page id -> (the page's path on this backend, the key that opens it).
#: Must match cpanel/core/oms_session.ALLOWED_PATHS.
PAGES = {
    'oils-sale':         ('/realise/', OILS_SALE),
    'sales-channel':     ('/realise/sales-channel/', SALES),
    'beverages-sale':    ('/realise/beverages/', SALES),
    'realise-dashboard': ('/realise/realise-dashboard/', SALES),
    'targets':           ('/realise/targets/', SALES),
    'sales':             ('/sales/', SALES),
    'inventory':         ('/inventory/', INVENTORY),
    'expenses':          ('/expenses/', FINANCE),
    'salaries':          ('/salaries/', FINANCE),
    **{r[3]: (r[4], REPORT_KEY[r[0]]) for r in REPORTS},
}

#: page path -> key, for the server-side page guard.
PATH_KEYS = {path: key for path, key in PAGES.values()}


def cp_keys(user):
    """The Control Panel keys `user` holds (all of them, for an admin)."""
    return sorted(set(ALL_CP_KEYS) & effective_keys(user))


def allowed_tabs(user, key, tabs):
    """All of a page's `tabs` when `user` holds its `key`, else none."""
    return list(tabs) if key in effective_keys(user) else []


class CanOpenAnyControlPanelPage(HasAnyKey):
    """The endpoint gate; the per-page check is in the view."""

    def __init__(self):
        super().__init__(*ALL_CP_KEYS)
