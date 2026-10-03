import logging
import json
import threading
from datetime import date

from django.core.cache import cache

logger = logging.getLogger(__name__)

GROUP_PROFILE = {
    'realise_admin':     ('admin',  '',          True),
    'realise_premium':   ('viewer', 'PREMIUM',   False),
    'realise_commodity': ('viewer', 'COMMODITY', False),
}

INVENTORY_GROUPS = {'inventory_admin', 'inventory_viewer'}
REALISE_GROUPS = {'realise_admin', 'realise_premium', 'realise_commodity'}

MODULE_GROUPS = {
    'sales': {'sales_admin', 'sales_viewer'},
    'expenses': {'expenses_admin', 'expenses_viewer'},
    'salaries': {'salaries_admin', 'salaries_viewer', 'salary_admin', 'salary_viewer'},
    'cogs': {'cogs_admin', 'cogs_viewer'},
}


def get_user_groups(user):
    """Inside OMS: the C_Panel groups the user's OMS keys amount to (oms_access)."""
    from cpanel.core.oms_access import cp_groups
    return sorted(cp_groups(user))


def get_group_permission_codenames(user):
    """Inside OMS access comes from OMS keys, never Django model permissions."""
    return []


def get_permission_codenames(user):
    """Inside OMS access comes from OMS keys, never Django model permissions."""
    return []


def build_user_permissions(user):
    """C_Panel's `can_*` flags, decided by the user's OMS keys (oms_access).

    Replaces C_Panel's group-and-permission logic. Every flag still exists, so
    every template and decorator keeps working; the ones for pages OMS does not
    show are always False.
    """
    from cpanel.core.oms_access import cp_flags
    return cp_flags(user)


def _oms_display_name(user):
    from cpanel.core.oms_access import display_name
    return display_name(user)


def build_login_permission_payload(user):
    role, type_filter, _ = derive_realise_profile(user)
    permissions = build_user_permissions(user)
    return {
        'id': user.pk,
        'username': user.get_username(),
        'display_name': _oms_display_name(user),
        'email': user.email or '',
        # Never true here: an OMS admin is not a C_Panel superuser (oms_access).
        'is_staff': False,
        'is_superuser': False,
        'groups': get_user_groups(user),
        'group_permission_codenames': get_group_permission_codenames(user),
        'permission_codenames': get_permission_codenames(user),
        'role': role,
        'type_filter': type_filter,
        'permissions': permissions,
        **permissions,
    }


def get_login_permission_payload(request):
    user = request.user
    if not user.is_authenticated:
        permissions = build_user_permissions(user)
        return {
            'id': None,
            'username': '',
            'display_name': '',
            'email': '',
            'is_staff': False,
            'is_superuser': False,
            'groups': [],
            'group_permission_codenames': [],
            'permission_codenames': [],
            'role': '',
            'type_filter': '',
            'permissions': permissions,
            **permissions,
        }

    payload = build_login_permission_payload(user)
    request.session['group_permissions'] = payload
    return payload


def _build_period_options():
    today = date.today()
    options = []
    y, m = today.year, today.month
    for _ in range(13):
        options.append({
            'value': f'{y}-{m:02d}',
            'label': date(y, m, 1).strftime('%B %Y'),
        })
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return options


def _resolve_period(request):
    raw = request.GET.get('period', '')
    today = date.today()
    try:
        parts = raw.split('-')
        y, m = int(parts[0]), int(parts[1])
        if 1 <= m <= 12 and 2000 <= y <= 2100:
            return y, m
    except (ValueError, IndexError, AttributeError):
        pass
    return today.year, today.month


# How long a built ticker stays good for.
TICKER_TTL = 300
# A failed build is remembered only briefly, so one SAP hiccup does not blank
# the ticker for five minutes - but ten visitors still do not all retry at once.
TICKER_FAIL_TTL = 30

_ticker_locks = {}
_ticker_guard = threading.Lock()


def _ticker_key(year, month):
    return f'home_ticker_{year}_{month:02d}'


def _ticker_lock(key):
    with _ticker_guard:
        lock = _ticker_locks.get(key)
        if lock is None:
            lock = _ticker_locks[key] = threading.Lock()
        return lock


def get_ticker_items(year, month, blocking=False):
    """The values shown in the top strip.

    blocking=False is what page rendering uses: hand back whatever is already
    cached, otherwise an empty list straight away. A page must never sit and
    wait for SAP - the browser asks for the numbers separately.

    blocking=True is what the /api/nav-ticker/ endpoint uses: actually build
    the thing. Only one thread builds a given month at a time, so ten visitors
    arriving together cause one SAP pull, not ten.
    """
    key = _ticker_key(year, month)
    cached = cache.get(key)
    if cached is not None:
        return cached
    if not blocking:
        return []

    with _ticker_lock(key):
        cached = cache.get(key)
        if cached is not None:
            return cached
        items = _build_ticker_items(year, month)
        cache.set(key, items, TICKER_TTL if items else TICKER_FAIL_TTL)
        return items


def _build_ticker_items(year, month):
    items = []
    try:
        from cpanel.home import services as home_services
        sales = home_services.get_total_sales_volume(year, month)
        if sales.get('has_data'):
            items.append({'label': 'SALES', 'value': sales.get('value', '—')})
        realise = home_services.get_avg_realisation(year, month)
        if realise.get('has_data'):
            items.append({'label': 'REALISE', 'value': realise.get('value', '—')})
        for key, label in (('opex', 'OPEX'), ('salaries', 'SALARY'),
                           ('cogs', 'COGS'), ('inventory', 'INVENTORY')):
            getter = getattr(home_services, {
                'opex':      'get_operating_expenses',
                'salaries':  'get_salary_expenditure',
                'cogs':      'get_cost_of_goods_sold',
                'inventory': 'get_inventory_value',
            }[key])
            kpi = getter(year, month)
            if kpi.get('has_data'):
                items.append({'label': label, 'value': kpi.get('value', '—')})
    except Exception as e:
        logger.warning('[context] ticker build failed: %s', e)
    return items


def _filter_ticker_items(items, permissions):
    allowed_by_label = {
        'SALES': permissions.get('can_sales'),
        'REALISE': permissions.get('can_realise'),
        'AVG REALISE': permissions.get('can_realise'),
        'OPEX': permissions.get('can_expenses'),
        'TOTAL OPEX': permissions.get('can_expenses'),
        'SALARY': permissions.get('can_salaries'),
        'COGS': permissions.get('can_cogs'),
        'INVENTORY': permissions.get('can_inventory'),
    }
    return [
        item for item in items
        if allowed_by_label.get(item.get('label'), True)
    ]


def derive_realise_profile(user):
    """(role, type_filter, can_edit) for the Realise pages, from OMS keys. The
    first role held wins, in C_Panel's order: admin, premium, commodity."""
    if not user.is_authenticated:
        return ('anonymous', '', False)
    from cpanel.core.oms_access import cp_groups
    user_groups = cp_groups(user)
    for group_name in ('realise_admin', 'realise_premium', 'realise_commodity'):
        if group_name in user_groups:
            return GROUP_PROFILE[group_name]
    return ('viewer', '', False)


def user_profile(request):
    user = request.user

    year, month = _resolve_period(request)
    # Never block the page on SAP: use the cached strip if we have one, else
    # render empty and let the browser fetch it from /api/nav-ticker/.
    ticker_items = get_ticker_items(year, month, blocking=False)

    period_ctx = {
        'period_year':     year,
        'period_month':    month,
        'period_label':    date(year, month, 1).strftime('%B %Y'),
        'period_options':  _build_period_options(),
        'selected_period': f'{year}-{month:02d}',
        'ticker_items':    ticker_items,
    }

    login_payload = get_login_permission_payload(request)
    group_permissions = login_payload['permissions']
    user_groups = login_payload['groups']
    group_permission_codenames = login_payload['group_permission_codenames']
    permission_codenames = login_payload['permission_codenames']

    if not user.is_authenticated:
        return {
            'is_authenticated': False,
            'login_user': login_payload,
            'login_user_json': json.dumps(login_payload),
            'display_name': login_payload['display_name'],
            'role': login_payload['role'],
            'type_filter': login_payload['type_filter'],
            'user_groups': user_groups,
            'user_groups_json': json.dumps(user_groups),
            'group_permission_codenames': group_permission_codenames,
            'group_permission_codenames_json': json.dumps(group_permission_codenames),
            'permission_codenames': permission_codenames,
            'permission_codenames_json': json.dumps(permission_codenames),
            'group_permissions': group_permissions,
            'group_permissions_json': json.dumps(group_permissions),
            **group_permissions,
            **period_ctx,
        }

    period_ctx['ticker_items'] = _filter_ticker_items(period_ctx['ticker_items'], group_permissions)

    return {
        'is_authenticated': True,
        'login_user': login_payload,
        'login_user_json': json.dumps(login_payload),
        'display_name': login_payload['display_name'],
        'role': login_payload['role'],
        'type_filter': login_payload['type_filter'],
        'user_groups': user_groups,
        'user_groups_json': json.dumps(user_groups),
        'group_permission_codenames': group_permission_codenames,
        'group_permission_codenames_json': json.dumps(group_permission_codenames),
        'permission_codenames': permission_codenames,
        'permission_codenames_json': json.dumps(permission_codenames),
        'group_permissions': group_permissions,
        'group_permissions_json': json.dumps(group_permissions),
        **group_permissions,
        **period_ctx,
    }
