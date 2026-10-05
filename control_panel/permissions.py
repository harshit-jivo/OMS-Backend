"""Who may open which Control Panel page.

The Control Panel is four pages, as the OMS sidebar shows it, each one a
single permission that opens the page with ALL of its sub-pages:

    Oils Sale   Overview · Map · Realise
    Sales       Sales Channel · Beverages Sale · Realise Dashboard · Sales · Targets
    Inventory   Dashboard · Stock & Warehouses · Stock Movement · Moving / Non-Moving ·
                FG Not Billed · ABC-XYZ Analysis · Aging Analysis · Item Trace ·
                Inventory Planning   (rupee values shown)
    Finance     Expenses · Salaries

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

#: Oils Sale's inner tabs and Inventory's sections (the pages' own ids / OMS `?tab=`).
OILS_TABS = ('overview', 'map', 'realise')
INVENTORY_TABS = ('dash', 'stock', 'move', 'movers', 'billing', 'abc', 'aging', 'trace', 'planning')

#: Every Control Panel key.
ALL_CP_KEYS = tuple(TREE)

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
