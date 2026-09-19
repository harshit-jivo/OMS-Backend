"""Verify SAP_CASH_PARENT_ACCOUNT against each company's SAP database.

Read-only. Run it after setting or changing the value, and after a
chart-of-accounts change:

    python manage.py validate_cash_parent
    python manage.py validate_cash_parent --company OIL

It answers three questions per company:

  * does the configured node exist in that company database?
  * is it a SUMMARY account (Postable='N')? A postable node would put the
    group header itself in the dropdown;
  * how many selectable drawers hang off it, and which?

Deliberately a command rather than a startup check: it needs HANA, and booting
the application must never depend on SAP being reachable (see checks.py).
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from payments import bank_master, hana_queries

COMPANIES = ('OIL', 'BEVERAGES', 'MART')


class Command(BaseCommand):
    help = "Check SAP_CASH_PARENT_ACCOUNT against each company's SAP database."

    def add_arguments(self, parser):
        parser.add_argument(
            '--company', choices=COMPANIES,
            help='Check one company instead of all three.')

    def handle(self, *args, **options):
        parent = str(
            getattr(settings, 'SAP_CASH_PARENT_ACCOUNT', '') or '').strip()
        if not parent:
            self.stderr.write(self.style.ERROR(
                'SAP_CASH_PARENT_ACCOUNT is not set. Nothing to validate.'))
            return

        self.stdout.write(f'Cash account node: {parent}')
        companies = [options['company']] if options.get('company') else list(
            COMPANIES)

        problems = 0
        for company in companies:
            self.stdout.write('')
            self.stdout.write(self.style.MIGRATE_HEADING(company))
            try:
                row = hana_queries.fetch_account_classification(
                    company=company, gl_account=parent)
            except Exception as error:                        # noqa: BLE001
                problems += 1
                self.stderr.write(self.style.ERROR(
                    f'  could not read the account: {error}'))
                continue

            if row is None:
                problems += 1
                self.stderr.write(self.style.ERROR(
                    f'  {parent} does not exist in this company database.'))
                continue

            self.stdout.write(
                f'  {row["gl_account"]} {row["account_name"]} '
                f'(Postable={row["postable"]}, Frozen={row["frozen"]}, '
                f'Levels={row["levels"]})')

            if row['postable'] == 'Y':
                problems += 1
                self.stderr.write(self.style.ERROR(
                    '  this node is POSTABLE — a summary account was '
                    'expected, so the group header itself would be '
                    'selectable.'))

            # A frozen CHILD is fine and simply drops out of the list; only the
            # configured node itself has to be sound.
            accounts, meta = bank_master.get_company_cash_accounts(
                company, force_refresh=True)
            if not meta['available']:
                problems += 1
                self.stderr.write(self.style.ERROR(
                    '  cash accounts could not be read from SAP.'))
                continue

            if not accounts:
                problems += 1
                self.stderr.write(self.style.WARNING(
                    '  no selectable cash accounts hang off this node.'))
            for account in accounts:
                self.stdout.write(
                    f'    {account["gl_account"]} - {account["account_name"]}')

        self.stdout.write('')
        if problems:
            self.stderr.write(self.style.ERROR(
                f'{problems} problem(s) found.'))
        else:
            self.stdout.write(self.style.SUCCESS(
                'Cash account configuration is valid.'))
