"""Give OMS users the Control Panel access they have in production C_Panel.

Reads C_Panel's users and their groups from its db.sqlite3 and grants each
matching OMS user (same username, case-insensitive) the OMS keys those groups
amount to (control_panel/permissions.py). Grants are ADDED to the user's
personal grants (`extra_pages`); nothing is taken away.

C_Panel's group -> OMS key mapping follows C_Panel's own flag rules
(core/context_processors.build_user_permissions there): a Realise role opens
Oils Sale and Sales; the inventory groups also opened Stock Available,
FG Non-Moving, Reconciliation, Production and Daily Production; the Realise
Calculator's group also opened Rate List and Plan vs Done; Customer Aging's
also opened Beverages GST; stock_viewer also opened FG Non-Moving;
production_viewer also opened Daily Production.

Not copied, and listed instead: C_Panel users with no OMS account, superusers
(make them OMS admins by hand if wanted), and users whose access came from
direct Django permissions rather than groups.

    python manage.py import_cpanel_access C:/path/to/db.sqlite3            # dry run
    python manage.py import_cpanel_access C:/path/to/db.sqlite3 --apply
"""
import sqlite3

from django.contrib.auth import get_user_model
from django.core.management import BaseCommand, CommandError
from django.db import transaction

from control_panel import permissions as cp
from core.permissions import is_admin

R = cp.REPORT_KEY
INVENTORY_REPORTS = [R['stock_available'], R['non_inventory'], R['reconciliation'],
                     R['production'], R['daily_production']]

#: C_Panel group -> the OMS keys it amounts to.
GROUP_KEYS = {
    'realise_admin': [cp.OILS_SALE, cp.SALES],
    'realise_premium': [cp.OILS_SALE, cp.SALES],
    'realise_commodity': [cp.OILS_SALE, cp.SALES],
    'sales_viewer': [cp.SALES],
    'inventory_viewer': [cp.INVENTORY, *INVENTORY_REPORTS],
    'inventory_admin': [cp.INVENTORY, *INVENTORY_REPORTS],
    'expenses_viewer': [cp.FINANCE],
    'salaries_viewer': [cp.FINANCE],
    'compare_sales_viewer': [R['compare_sales']],
    'sales_cn_viewer': [R['sales_cn']],
    'hidden_sales_viewer': [R['hidden_sales']],
    'sales_flow_viewer': [R['sales_flow']],
    'dispatch_details_viewer': [R['dispatch_details']],
    'realise_calculator_viewer': [R['realise_calculator'], R['rate_list'], R['plan_vs_done']],
    'customer_aging_viewer': [R['customer_aging'], R['beverages_gst']],
    'required_credit_viewer': [R['required_credit_limit']],
    'open_payments_viewer': [R['open_payments']],
    'claims_viewer': [R['claims']],
    'reconciliation_viewer': [R['reconciliation']],
    'stock_viewer': [R['stock_available'], R['non_inventory']],
    'non_inventory_viewer': [R['non_inventory']],
    'oih_vs_stock_viewer': [R['oih_vs_stock']],
    'production_viewer': [R['production'], R['daily_production']],
    'daily_production_viewer': [R['daily_production']],
    'customer_master_viewer': [R['customer_master']],
}


class Command(BaseCommand):
    help = "Grant OMS users the Control Panel access they have in production C_Panel."

    def add_arguments(self, parser):
        parser.add_argument('sqlite_path')
        parser.add_argument('--apply', action='store_true', help='Write (default: dry run).')

    def handle(self, sqlite_path, apply, **options):
        try:
            src = sqlite3.connect(f'file:{sqlite_path}?mode=ro', uri=True)
            users = src.execute('select id, username, is_superuser, is_active from auth_user').fetchall()
        except sqlite3.Error as exc:
            raise CommandError(f'Not a C_Panel database: {sqlite_path} ({exc})')
        groups = {}
        for uid, name in src.execute('select ug.user_id, g.name from auth_user_groups ug '
                                     'join auth_group g on g.id = ug.group_id'):
            groups.setdefault(uid, set()).add(name)
        direct = {uid for (uid,) in src.execute('select distinct user_id from auth_user_user_permissions')}

        User = get_user_model()
        oms = {u.username.lower(): u for u in User.objects.all()}
        plan, no_account, supers, unknown_groups, oms_admins = [], [], [], set(), []
        for uid, username, is_super, is_active in users:
            if is_super:
                supers.append(username)
            held = groups.get(uid, set())
            unknown_groups |= held - set(GROUP_KEYS)
            keys = [k for g in sorted(held) for k in GROUP_KEYS.get(g, [])]
            if not keys:
                continue
            target = oms.get(username.lower())
            if target is None:
                no_account.append(username)
                continue
            if is_admin(target):  # holds every key already
                oms_admins.append(username)
                continue
            new = [k for k in dict.fromkeys(keys) if k not in (target.extra_pages or [])]
            plan.append((target, new, sorted(held)))

        for user, new, held in plan:
            self.stdout.write(f'{user.username}: C_Panel {held} -> adds {new or "nothing (already held)"}')
        if oms_admins:
            self.stdout.write(f'Already OMS admins (hold every key), skipped: {sorted(oms_admins)}')
        if no_account:
            self.stdout.write(f'No OMS account (create in App User, then rerun): {sorted(no_account)}')
        if supers:
            self.stdout.write(f'C_Panel superusers (make OMS admins by hand if wanted): {sorted(supers)}')
        manual = sorted(n for i, n, *_ in users if i in direct)
        if manual:
            self.stdout.write(f'Access from direct Django permissions, check by hand: {manual}')
        if unknown_groups:
            self.stdout.write(f'Groups with no OMS equivalent (ignored): {sorted(unknown_groups)}')

        if not apply:
            self.stdout.write(self.style.WARNING('Dry run: nothing written. Add --apply to grant.'))
            return
        with transaction.atomic():
            for user, new, _ in plan:
                if new:
                    user.extra_pages = list(user.extra_pages or []) + new
                    user.save(update_fields=['extra_pages'])
        self.stdout.write(self.style.SUCCESS(f'Granted to {sum(1 for _, n, _ in plan if n)} user(s).'))
