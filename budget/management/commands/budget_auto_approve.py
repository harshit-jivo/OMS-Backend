"""Auto-approve items that have waited long enough, within SAP's budget. Run every 10 minutes.

    python manage.py budget_auto_approve            # does nothing unless switched on in settings
    python manage.py budget_auto_approve --dry-run  # say what would be approved

See `budget/services/auto.py` for the rule.
"""
import json
from dataclasses import asdict

from django.core.management.base import BaseCommand, CommandError

from budget.services import auto


class Command(BaseCommand):
    help = 'Auto-approve budget items waiting past the configured hours, within the month\'s SAP budget.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        result = auto.run(dry_run=opts['dry_run'])
        self.stdout.write(json.dumps(asdict(result), default=str, indent=2))
        if result.errors:
            raise CommandError('Some items could not be auto-approved; see above.')
