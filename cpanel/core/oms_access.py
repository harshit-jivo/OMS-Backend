"""Who may do what on the Control Panel pages — decided by OMS permission keys.

C_Panel decided access from Django groups (realise_admin, sales_viewer, ...)
and the `can_*` flags `build_user_permissions` derived from them. Inside OMS
there are no such groups: an administrator grants the Control Panel's four
pages, one permission each, on the OMS Permissions page (control_panel/permissions.py).
This module turns a user's keys into the group names C_Panel's code expects,
so the rest of that code — every `group_required`, `permission_flag_required`
and `{% if can_* %}` — runs unchanged.

    OMS page key                 C_Panel groups
    ───────────────────────────  ─────────────────────────────────────────────
    Control Panel — Oils Sale    realise_admin (all segments, edits targets)
    Control Panel — Sales        realise_admin + sales_viewer
    Control Panel — Inventory    inventory_viewer + inventory_admin (rupee values shown)
    Control Panel — Finance      expenses_viewer + salaries_viewer

Which PAGE of a group a user may open is the page guard's job
(cpanel/core/page_guard.py); which sub-tabs a page shows, the templates'
(`cp_oils_tabs` / `cp_inventory_tabs` from cpanel/core/context.py — all of
them for a page holder).

An OMS administrator holds every key, so gets every group above — and nothing
beyond them. An OMS superuser is NOT a C_Panel superuser: the C_Panel report
pages OMS does not show stay closed.
"""
from control_panel import permissions as cp
from core.permissions import effective_keys

REALISE_GROUPS = {'realise_admin', 'realise_premium', 'realise_commodity'}
INVENTORY_GROUPS = {'inventory_admin', 'inventory_viewer'}

#: Every `can_*` flag C_Panel's templates and decorators read. Flags for pages
#: OMS does not show are always False.
ALL_FLAGS = (
    'can_edit', 'can_realise', 'can_inventory', 'inventory_can_edit',
    'can_stock_available', 'can_non_inventory', 'can_reconciliation', 'can_production',
    'can_oih_vs_stock', 'can_daily_production', 'can_compare_sales', 'can_sales_cn',
    'can_hidden_sales', 'can_customer_master', 'can_open_payments', 'can_dispatch_details',
    'can_realise_calculator', 'can_sales_flow', 'can_claims', 'can_customer_aging',
    'can_required_credit_limit', 'can_sales', 'can_expenses', 'can_salaries', 'can_cogs',
)


def cp_groups(user):
    """The C_Panel group names `user`'s OMS keys amount to."""
    if not user or not getattr(user, 'is_authenticated', False):
        return set()
    keys = effective_keys(user)
    groups = set()
    if cp.OILS_SALE in keys or cp.SALES in keys:
        groups.add('realise_admin')
    if cp.SALES in keys:
        groups.add('sales_viewer')
    if cp.INVENTORY in keys:
        groups |= {'inventory_viewer', 'inventory_admin'}
    if cp.FINANCE in keys:
        groups |= {'expenses_viewer', 'salaries_viewer'}
    return groups


def cp_flags(user):
    """C_Panel's `can_*` flags for `user`, from their OMS keys."""
    flags = dict.fromkeys(ALL_FLAGS, False)
    groups = cp_groups(user)
    if not groups:
        return flags
    flags.update({
        'can_edit': 'realise_admin' in groups,
        'can_realise': bool(groups & REALISE_GROUPS),
        'can_inventory': bool(groups & INVENTORY_GROUPS),
        'inventory_can_edit': 'inventory_admin' in groups,
        'can_sales': 'sales_viewer' in groups,
        'can_expenses': 'expenses_viewer' in groups,
        'can_salaries': 'salaries_viewer' in groups,
    })
    return flags


def display_name(user):
    """OMS users have `name`, not first/last name."""
    return (getattr(user, 'name', '') or user.get_username()) if user.is_authenticated else ''


def is_oms_admin(user):
    from core.permissions import is_admin
    return bool(user and user.is_authenticated and is_admin(user))
