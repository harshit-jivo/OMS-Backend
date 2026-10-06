"""Take budget-gated SAP drafts into OMS. Run every 5 minutes.

    python manage.py sync_budget_drafts                  # every switched-on company
    python manage.py sync_budget_drafts --company OIL
    python manage.py sync_budget_drafts --dry-run        # route for real, then roll back

Companies come from `BUDGET_SAP_SCHEMAS`; none configured is a no-op.
Exits non-zero when SAP cannot be read, or a draft failed — a scheduler then
shows the failure instead of "succeeded" (JSAP's lesson).
"""
import json

from django.core.management.base import BaseCommand, CommandError

from budget.services import sap as sap_service
from budget.services import sync as sync_service


class Command(BaseCommand):
    help = 'Take SAP drafts waiting for budget approval into OMS, and notice changed or departed ones.'

    def add_arguments(self, parser):
        parser.add_argument('--company', help='One company code (default: every one in BUDGET_SAP_SCHEMAS).')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        companies = [opts['company'].upper()] if opts['company'] else sorted(sap_service.schemas())
        if not companies:
            self.stdout.write('BUDGET_SAP_SCHEMAS is not set: budget approval is switched off here.')
            return
        failed = False
        for company in companies:
            try:
                result = sync_service.run(company, dry_run=opts['dry_run'])
            except sap_service.SapUnavailable as exc:
                self.stderr.write(self.style.ERROR(f'{company}: {exc}'))
                failed = True
                continue
            self.stdout.write(json.dumps(result.as_dict(), default=str, indent=2))
            if result.quiet:
                self.stdout.write(self.style.WARNING(f'{company}: SAP has no drafts waiting for budget approval.'))
            failed = failed or bool(result.errors)
        if failed:
            raise CommandError('The budget sync did not complete cleanly; see above.')
