"""The permission registry — every grantable capability, declared in one place.

This file is the single source of truth for permission KEYS. Everything else
derives from it:

* `core.permissions.HasKey` refuses (at import time) a key not listed here, so
  a typo in a view is a crash on boot, not a silent hole that greps clean.
* `core.permissions.effective_keys` intersects a user's stored grants with
  this registry, so a stale or mistyped key in the database is inert.
* The Role Permissions matrix page (Phase 4) renders its rows from this dict —
  a key not registered here cannot be ticked, and a key removed here silently
  stops granting.

Adding a module to the app means adding its keys here and gating its views
with `HasKey`. There is no second mechanism.

Two naming generations live side by side, deliberately
-------------------------------------------------------
* NEW keys are `module.resource.action` (e.g. `orders.sales.create`). Dots
  and lowercase mark them apart at a glance.
* LEGACY keys are the flat `extra_pages` strings the web Permissions page and
  the mobile app already exchange (`App_User`, `Payments_Create`, ...). They
  are registered verbatim because renaming them would break the mobile
  contract for zero gain; a legacy key is just a registry entry with an old
  name.

The registry maps keys to labels only. It does NOT parse keys back into
app/action/model — EXIM derives structure by splitting permission strings on
underscores, which silently mangles every custom name (`sync_rm` becomes
resource "rm") and forced a growing alias table in its frontend. Keys here
are opaque identifiers; the grouping is explicit.
"""

# ---------------------------------------------------------------------------
# module -> { key -> human label }
#
# The module name is presentation grouping for the matrix UI, nothing more.
# ---------------------------------------------------------------------------

REGISTRY: dict[str, dict[str, str]] = {

    # --- Orders / sales flow (new-style keys; Phase 3 points the order
    # --- endpoints at these, replacing the HasAnyRole role names) -----------
    'orders': {
        'orders.sales.view':        'View sales orders',
        'orders.sales.create':      'Create sales orders',
        'orders.sales.edit':        'Edit sales orders',
        'orders.decision.simple':   'Approve/reject orders (simple flow)',
        'orders.status.transition': 'Move orders through the status flow',
        'orders.mart.decide':       'Approve/reject Mart (distributor) orders',
        # Company-wide visibility, as opposed to `orders.sales.view`, which is
        # the right to see the orders already IN YOUR SCOPE. `_get_base_orders`
        # decides that scope, and it did so by matching `user.role.name`
        # against seven literals -- so any role outside that list fell through
        # to `Order.objects.none()` and its holder got a dashboard of zeroes
        # rather than a refusal. This key is how a role says "no scoping",
        # without a new literal having to be added to that function each time
        # an administrator invents a role.
        'orders.sales.view_all':    'View every order, company-wide',
    },

    # --- Admin pages (legacy extra_pages keys, verbatim from
    # --- Frontend/src/config/adminPages.ts GRANTABLE_ADMIN_PAGES) -----------
    'pages': {
        'App_User':                 'App User',
        'Sap_Sync':                 'SAP Sync',
        'Party_Assignment':         'Party Assignment',
        'Party_Product_Assignment': 'Party Product Assignment',
        'Add_Scheme':               'Add Scheme',
        'Scheme_Manager':           'Schemes',
        'Combo_Mapping':            'Combo Mapping',
        'Order_Flow_Settings':      'Order Flow Settings',
        'Product_Stock':            'Stock',
        'Reports':                  'Reports',
        'Einvoice':                 'e-Invoice (IRN)',
        'Ewaybill':                 'e-Way Bill',
        'HAIS':                     'Hardware Assets (HAIS)',
        'Distributor':              'Distributor',
        'Mart_Approval':            'Mart Approval',
        'Device_Management':        'Device Management',
        # Staff orders — an internal order raised against an employee ID
        # rather than a party, priced from each product's staff rate. Two
        # keys, not one: reading the staff catalogue to PLACE an order and
        # setting the rates the company sells to its own people at are
        # different authorities, and the second is the one worth withholding.
        #
        # Both were `adminOnly` on the route and grantable to nobody, while
        # `orders/staff-products/` carried no permission_classes at all — so
        # the page was admin-only and the endpoint behind it took writes from
        # any signed-in user. These keys are what closes that.
        'Staff':                    'Staff Orders',
        'Staff_Rate_Assignment':    'Staff Rate Assignment',
        # The order/revenue analytics screen, formerly `/Dashboard`. It was
        # ungated for one structural reason — it doubled as the landing page,
        # so denying it would have looped the user — and `/Home` taking that
        # job is what let it become a normal page with a normal key.
        # Back-granted to today's dashboard roles by users/0034.
        'Sales_Dashboard':          'Sales Dashboard',
        # One key for the whole Legal module — Label Checker and Nutrition
        # Manager are one desk, the way `Distributor` covers both distributor
        # routes. Gates every legal/ endpoint via HasKeyOrRole, with the
        # `legal` role as the transitional fallback (see legal/views.py).
        'Legal':                    'Legal (Labels & Nutrition)',
    },

    # --- Payments actions (legacy keys, verbatim from
    # --- payments/permissions.py; the mobile app checks these strings) ------
    'payments': {
        'Payments_Create':          'Payments — Create',
        # The handover gate between creation and approval: a second person
        # checks the physical cash/cheque against the entry. Independent of
        # Create and Approve — holding either confers nothing here, and the
        # creator of a receipt may never verify it (separation of duties is
        # enforced in the endpoint, not by this key).
        'Payments_Verify':          'Payments — Verify (handover)',
        'Payments_Approve':         'Payments — Approve',
        'Deposit_Create':           'Deposit — Create',
        'Deposit_Approve':          'Deposit — Approve',
        'Payments_Dashboard':       'Payments Dashboard',
    },

    # --- Invoices (new-style keys; the frontend invoice routes accept these
    # --- alongside the legacy billing/factory_approver roles) ---------------
    'invoices': {
        'invoices.sales.create':    'Sales Invoice — create and print',
        'invoices.review.decide':   'Invoice Review — approve/reject',
        'invoices.report.view':     'Invoice Report',
    },

    # --- Document tracker (page keys, verbatim from
    # --- tracker/permissions.py; role map still grants them too) ------------
    # A key ticked here is UNIONED with what the four tracker sub-roles
    # already confer through ROLE_PAGE_MAP — see `tracker_pages_for`. So a
    # manager can be granted the Reports page without holding a tracker role,
    # and the sub-roles keep working untouched.
    'tracker': {
        'Tracker_Entry':            'Tracker — Invoice Entry',
        'Tracker_Queue':            'Tracker — My Stage Queue',
        'Tracker_Invoices':         'Tracker — All Invoices',
        'Tracker_Alerts':           'Tracker — Stuck Alerts',
        'Tracker_Reports':          'Tracker — Reports',
        'Tracker_Admin':            'Tracker — Administration',
        'Ap_Invoice_Entry':         'AP Invoice Entry (vendor invoices)',
    },

    # HANA has no module here, deliberately: `hana/` endpoints are read-only
    # data feeds behind pages that already carry keys — Product_Stock and
    # Reports gate every screen they serve. A `hana.*` key would gate nothing
    # those keys do not already gate. If per-report granularity is ever wanted
    # (splitting the umbrella `Reports` key the way EXIM split its balance
    # sheet), the new keys belong in `pages`, with a back-grant migration.
}

#: Every registered key, for membership tests. `frozenset` because nothing may
#: mutate the registry at runtime — it changes by code review or not at all.
ALL_KEYS: frozenset[str] = frozenset(
    key for keys in REGISTRY.values() for key in keys
)

# Duplicate keys across modules would make the matrix UI ambiguous and the
# labels a lottery. Cheap to check once at import.
assert sum(len(keys) for keys in REGISTRY.values()) == len(ALL_KEYS), (
    'permission_registry: the same key is registered under two modules'
)
