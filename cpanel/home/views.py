import json
import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import render, redirect
from django.views.decorators.http import require_http_methods

from cpanel.core.context_processors import build_user_permissions


logger = logging.getLogger(__name__)

# ── Landing ───────────────────────────────────────────────────────────────
# The Control Panel home page is gone - "/" is the Sales page now. It stays a redirect
# rather than a straight URL swap so every existing bookmark, {% url 'home' %} and
# LOGIN_REDIRECT_URL keeps working instead of 404-ing.
#
# Not everyone can open Sales, and group_required answers a user without access with a bare
# 403 that carries no navigation at all - which would strand them at the front door. So each
# user is handed the first page they are actually allowed to see, in sidebar order.
LANDING_ROUTES = [
    # Sales first - almost everyone has it.
    ('can_realise',              'realise:dashboard'),
    ('can_inventory',            'inventory:dashboard'),
    # Then every other page in sidebar order. This list must cover EVERY page flag: a user
    # whose only access is, say, Claims used to fall past the end of a shorter list and be
    # told "your account has no pages enabled" - while a page they were allowed to open sat
    # right there in their sidebar. Add a row here whenever a new page is added.
    ('can_compare_sales',        'realise:compare_sales'),
    ('can_sales_cn',             'realise:sales_cn'),
    ('can_hidden_sales',         'realise:hidden_sales'),
    ('can_sales_flow',           'realise:sales_flow'),
    ('can_dispatch_details',     'realise:dispatch_details'),
    ('can_realise_calculator',   'realise:realise_calculator'),
    ('can_customer_aging',       'realise:customer_aging'),
    ('can_required_credit_limit','realise:required_credit_limit'),
    ('can_open_payments',        'realise:open_payments'),
    ('can_claims',               'realise:claims'),
    ('can_reconciliation',       'inventory:reconciliation'),
    ('can_stock_available',      'inventory:stock_available'),
    ('can_non_inventory',        'inventory:non_inventory'),
    ('can_oih_vs_stock',         'realise:oih_vs_stock'),
    ('can_production',           'inventory:production'),
    ('can_daily_production',     'inventory:daily_production'),
    ('can_customer_master',      'realise:customer_master'),
    ('can_expenses',             'dashboard:expenses'),
    ('can_salaries',             'dashboard:salaries'),
]


@login_required(login_url='/cp/signed-out/')
def landing(request):
    """Send "/" to the Sales page, or to the first page this user can actually open."""
    perms = build_user_permissions(request.user)
    for flag, route in LANDING_ROUTES:
        if perms.get(flag):
            return redirect(route)
    # (C_Panel sent staff to its User Management screen here. Inside OMS, users
    # and access are managed on OMS's Permissions page instead.)
    return HttpResponseForbidden(
        'Your account has no pages enabled yet. Please ask an administrator for access.')


# ── User management (create users & assign access without the Django admin) ─────────────────
# Each access toggle maps to a Django Group; build_user_permissions() turns group membership
# into the can_* flags the app already gates on, so changes take effect immediately.
REALISE_ROLES = [
    {'value': '',                  'label': 'No Realise access'},
    {'value': 'realise_premium',   'label': 'Realise — Premium (viewer)'},
    {'value': 'realise_commodity', 'label': 'Realise — Commodity (viewer)'},
    {'value': 'realise_admin',     'label': 'Realise — Admin (full + can edit targets)'},
]
REALISE_ROLE_GROUPS = {'realise_premium', 'realise_commodity', 'realise_admin'}
PAGE_PERMS = [                       # standalone report pages (independent of full Realise)
    {'group': 'customer_aging_viewer',  'label': 'Customer Aging'},
    {'group': 'oih_vs_stock_viewer',    'label': 'OIH vs Stock'},
    {'group': 'compare_sales_viewer',   'label': 'Compare Sales'},
    {'group': 'sales_cn_viewer',        'label': 'Sales vs Credit Notes'},
    {'group': 'hidden_sales_viewer',    'label': 'Hidden Customer Sales'},
    {'group': 'customer_master_viewer', 'label': 'Customer Master'},
    {'group': 'sales_flow_viewer',      'label': 'Sales Document Flow'},
    {'group': 'claims_viewer',          'label': 'Claims'},
    {'group': 'required_credit_viewer', 'label': 'Required Credit Limit'},
    {'group': 'open_payments_viewer',   'label': 'Open Payments'},
    {'group': 'dispatch_details_viewer', 'label': 'Dispatch Details'},
    {'group': 'realise_calculator_viewer', 'label': 'Realise Calculator'},
    {'group': 'reconciliation_viewer',  'label': 'Wellness–Mart Reconciliation'},
    {'group': 'stock_viewer',           'label': 'Stock Available'},
    {'group': 'production_viewer',       'label': 'Production Plan'},
    {'group': 'daily_production_viewer', 'label': 'Daily Production Transaction'},
]
MODULE_PERMS = [                     # whole-module read access
    {'group': 'inventory_viewer', 'label': 'Inventory'},
    {'group': 'sales_viewer',     'label': 'Sales'},
    {'group': 'expenses_viewer',  'label': 'Expenses'},
    {'group': 'salaries_viewer',  'label': 'Salaries'},
    {'group': 'cogs_viewer',      'label': 'COGS'},
]
PAGE_GROUPS = {p['group'] for p in PAGE_PERMS}
MODULE_GROUPS = {m['group'] for m in MODULE_PERMS}
MANAGED_GROUPS = REALISE_ROLE_GROUPS | PAGE_GROUPS | MODULE_GROUPS


def _is_user_admin(user):
    """Who may manage users: superusers and staff (per the chosen access model)."""
    return user.is_authenticated and (user.is_superuser or user.is_staff)


def _user_state(u):
    groups = set(u.groups.values_list('name', flat=True))
    role = next((g for g in ('realise_admin', 'realise_premium', 'realise_commodity') if g in groups), '')
    return {
        'id': u.id,
        'username': u.username,
        'first_name': u.first_name,
        'email': u.email,
        'is_active': u.is_active,
        'is_staff': u.is_staff,
        'is_superuser': u.is_superuser,
        'realise_role': role,
        'groups': sorted(groups & (PAGE_GROUPS | MODULE_GROUPS)),
    }


@login_required
def user_management(request):
    """Self-service user admin: create users and assign per-page access without the Django
    admin. Visible to staff/superusers only."""
    if not _is_user_admin(request.user):
        return redirect('home')
    User = get_user_model()
    qs = User.objects.order_by('-is_superuser', '-is_staff', 'username')
    if not request.user.is_superuser:        # staff don't manage (or even see) superusers
        qs = qs.exclude(is_superuser=True)
    users = [_user_state(u) for u in qs]
    catalog = {
        'realise_roles': REALISE_ROLES,
        'page_perms': PAGE_PERMS,
        'module_perms': MODULE_PERMS,
    }
    return render(request, 'home/user_management.html', {
        'sidebar_active': 'user_management',
        'um_users': users,
        'um_catalog': catalog,
        'um_is_super': request.user.is_superuser,
        'um_self_id': request.user.id,
    })


def _um_body(request):
    try:
        return json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, UnicodeDecodeError):
        return {}


@require_http_methods(['POST'])
def api_user_save(request):
    """Create or update one user + their access groups. Only superusers may set the Staff /
    Superuser flags or edit a superuser account."""
    if not _is_user_admin(request.user):
        return JsonResponse({'status': 'error', 'error': 'Not allowed'}, status=403)
    User = get_user_model()
    body = _um_body(request)
    requester_super = request.user.is_superuser
    uid = body.get('id')

    if uid:
        u = User.objects.filter(id=uid).first()
        if not u:
            return JsonResponse({'status': 'error', 'error': 'User not found'}, status=404)
        if u.is_superuser and not requester_super:
            return JsonResponse({'status': 'error', 'error': 'Only a superuser can edit a superuser account'}, status=403)
    else:
        username = (body.get('username') or '').strip()
        password = body.get('password') or ''
        if not username:
            return JsonResponse({'status': 'error', 'error': 'Username is required'}, status=400)
        if User.objects.filter(username__iexact=username).exists():
            return JsonResponse({'status': 'error', 'error': 'That username already exists'}, status=400)
        if not password:
            return JsonResponse({'status': 'error', 'error': 'A password is required for a new user'}, status=400)
        u = User(username=username)

    u.first_name = (body.get('first_name') or '').strip()[:150]
    u.email = (body.get('email') or '').strip()[:254]
    u.is_active = bool(body.get('is_active', True))
    # The Staff / Superuser flags are privileged — only a superuser may change them.
    if requester_super:
        u.is_superuser = bool(body.get('is_superuser'))
        u.is_staff = bool(body.get('is_staff')) or u.is_superuser   # superuser implies staff
    password = body.get('password') or ''
    if password:
        u.set_password(password)
    u.save()

    # Resolve the access toggles → the exact managed group set, preserving any unmanaged groups.
    target = set()
    role = body.get('realise_role') or ''
    if role in REALISE_ROLE_GROUPS:
        target.add(role)
    for g in (body.get('groups') or []):
        if g in (PAGE_GROUPS | MODULE_GROUPS):
            target.add(g)
    keep = set(u.groups.values_list('name', flat=True)) - MANAGED_GROUPS
    final = keep | target
    u.groups.set([Group.objects.get_or_create(name=n)[0] for n in final])

    return JsonResponse({'status': 'ok', 'user': _user_state(u)})


@require_http_methods(['POST'])
def api_user_delete(request):
    """Delete a user. Can't delete yourself; only a superuser can delete a superuser."""
    if not _is_user_admin(request.user):
        return JsonResponse({'status': 'error', 'error': 'Not allowed'}, status=403)
    User = get_user_model()
    body = _um_body(request)
    u = User.objects.filter(id=body.get('id')).first()
    if not u:
        return JsonResponse({'status': 'error', 'error': 'User not found'}, status=404)
    if u.id == request.user.id:
        return JsonResponse({'status': 'error', 'error': "You can't delete your own account"}, status=400)
    if u.is_superuser and not request.user.is_superuser:
        return JsonResponse({'status': 'error', 'error': 'Only a superuser can delete a superuser'}, status=403)
    u.delete()
    return JsonResponse({'status': 'ok'})
