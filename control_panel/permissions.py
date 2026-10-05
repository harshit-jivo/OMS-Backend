"""Who may open which Control Panel page — and which of its sub-tabs.

The Control Panel is four pages, as the OMS sidebar shows it, and every page
is granted per sub-tab:

    Oils Sale   Overview · Map · Realise
    Sales       Sales Channel · Beverages Sale · Realise Dashboard · Sales · Targets
    Inventory   Dashboard · Stock & Warehouses · Stock Movement · Moving / Non-Moving ·
                FG Not Billed · ABC-XYZ Analysis · Aging Analysis · Item Trace ·
                Inventory Planning   (+ the AI assistant, on top of a sub-tab)
    Finance     Expenses · Salaries

Holding any sub-tab of a page opens the page; the page then shows only the
sub-tabs held. Two scope keys narrow the Realise-based pages (all of Oils Sale
and every Sales sub-tab but "Sales") to one segment, as C_Panel's
`realise_premium` / `realise_commodity` roles did. Without either the holder
sees all segments and may edit targets (C_Panel's `realise_admin`).

cpanel/core/oms_access.py turns these keys into the C_Panel groups its code
checks; cpanel/core/page_guard.py refuses a page path to a user without its keys.
"""
from core.permissions import HasAnyKey, effective_keys

_P = 'control_panel.'

OILS_OVERVIEW = _P + 'oils_sale.overview'
OILS_MAP = _P + 'oils_sale.map'
OILS_REALISE = _P + 'oils_sale.realise'

SALES_CHANNEL = _P + 'sales.channel'
BEVERAGES = _P + 'sales.beverages'
REALISE_DASHBOARD = _P + 'sales.realise_dashboard'
SALES = _P + 'sales.sales'
TARGETS = _P + 'sales.targets'

INVENTORY_CHAT = _P + 'inventory.chat'

EXPENSES = _P + 'finance.expenses'
SALARIES = _P + 'finance.salaries'

PREMIUM_ONLY = _P + 'segment.premium_only'
COMMODITY_ONLY = _P + 'segment.commodity_only'

#: Oils Sale's inner tabs (the page's `data-pane` / OMS `?tab=` ids) -> key.
OILS_TABS = {'overview': OILS_OVERVIEW, 'map': OILS_MAP, 'realise': OILS_REALISE}

#: Inventory's sections (the page's `data-s` / OMS `?tab=` ids) -> key.
INVENTORY_TABS = {
    'dash': _P + 'inventory.dashboard',
    'stock': _P + 'inventory.stock',
    'move': _P + 'inventory.movement',
    'movers': _P + 'inventory.movers',
    'billing': _P + 'inventory.billing',
    'abc': _P + 'inventory.abc',
    'aging': _P + 'inventory.aging',
    'trace': _P + 'inventory.trace',
    'planning': _P + 'inventory.planning',
}

#: key -> label, per page, in sidebar order. The registry and the Page
#: Permissions screen both read this.
TREE = {
    'Oils Sale': {
        OILS_OVERVIEW: 'Overview',
        OILS_MAP: 'Map',
        OILS_REALISE: 'Realise',
    },
    'Sales': {
        SALES_CHANNEL: 'Sales Channel',
        BEVERAGES: 'Beverages Sale',
        REALISE_DASHBOARD: 'Realise Dashboard',
        SALES: 'Sales',
        TARGETS: 'Targets',
    },
    'Inventory': {
        INVENTORY_TABS['dash']: 'Dashboard',
        INVENTORY_TABS['stock']: 'Stock & Warehouses',
        INVENTORY_TABS['move']: 'Stock Movement',
        INVENTORY_TABS['movers']: 'Moving / Non-Moving',
        INVENTORY_TABS['billing']: 'FG Not Billed',
        INVENTORY_TABS['abc']: 'ABC-XYZ Analysis',
        INVENTORY_TABS['aging']: 'Aging Analysis',
        INVENTORY_TABS['trace']: 'Item Trace',
        INVENTORY_TABS['planning']: 'Inventory Planning',
        INVENTORY_CHAT: 'AI assistant',
    },
    'Finance': {
        EXPENSES: 'Expenses',
        SALARIES: 'Salaries',
    },
}

SCOPES = {
    PREMIUM_ONLY: 'Oils Sale & Sales — Premium segment only',
    COMMODITY_ONLY: 'Oils Sale & Sales — Commodity segment only',
}

#: Pages built on C_Panel's Realise data — the ones the segment scope narrows.
REALISE_KEYS = (*OILS_TABS.values(), SALES_CHANNEL, BEVERAGES, REALISE_DASHBOARD, TARGETS)

#: Keys that open something (scope keys only narrow).
ACCESS_KEYS = tuple(key for tabs in TREE.values() for key in tabs)
ALL_CP_KEYS = (*ACCESS_KEYS, *SCOPES)

#: OMS page id -> (the page's path on this backend, the keys any one of which
#: opens it). Must match cpanel/core/oms_session.ALLOWED_PATHS.
PAGES = {
    'oils-sale':         ('/realise/', tuple(OILS_TABS.values())),
    'sales-channel':     ('/realise/sales-channel/', (SALES_CHANNEL,)),
    'beverages-sale':    ('/realise/beverages/', (BEVERAGES,)),
    'realise-dashboard': ('/realise/realise-dashboard/', (REALISE_DASHBOARD,)),
    'targets':           ('/realise/targets/', (TARGETS,)),
    'sales':             ('/sales/', (SALES,)),
    'inventory':         ('/inventory/', tuple(INVENTORY_TABS.values())),
    'expenses':          ('/expenses/', (EXPENSES,)),
    'salaries':          ('/salaries/', (SALARIES,)),
}

#: page path -> keys, for the server-side page guard.
PATH_KEYS = {path: keys for path, keys in PAGES.values()}


def cp_keys(user):
    """The Control Panel keys `user` holds (all of them, for an admin)."""
    return sorted(set(ALL_CP_KEYS) & effective_keys(user))


def allowed_tabs(user, tabs):
    """The ids in `tabs` (OILS_TABS / INVENTORY_TABS) whose key `user` holds, in order."""
    keys = effective_keys(user)
    return [tab for tab, key in tabs.items() if key in keys]


class CanOpenAnyControlPanelPage(HasAnyKey):
    """The endpoint gate; the per-page check is in the view."""

    def __init__(self):
        super().__init__(*ACCESS_KEYS)
