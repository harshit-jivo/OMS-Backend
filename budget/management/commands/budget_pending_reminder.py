"""Remind each approver of the budget items waiting for them. Run once a day (e.g. 10:00).

    python manage.py budget_pending_reminder            # send
    python manage.py budget_pending_reminder --dry-run  # say who would be told what

One message per approver — today's stage user, stand-ins applied — pointing at
their oldest waiting item. JSAP's equivalent (`sendpendingbudgetnotify`) was an
endpoint nothing in its own code ever called.
"""
import json
from collections import defaultdict

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from budget.models import BudgetItem, ItemStatus
from budget.services import flow as flow_service
from budget.services import notify as notify_service


class Command(BaseCommand):
    help = 'Send each budget approver one reminder of the items waiting for them.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **opts):
        waiting = defaultdict(list)
        items = (BudgetItem.objects.filter(status=ItemStatus.PENDING, current_stage__isnull=False)
                 .select_related('draft'))
        for item in items:
            user_id = flow_service.effective_user_id(item.current_stage_id)
            if user_id:
                waiting[user_id].append(item)
        users = {u.pk: u for u in get_user_model().objects.filter(pk__in=list(waiting), is_active=True)}
        report = []
        for user_id, mine in sorted(waiting.items()):
            user = users.get(user_id)
            if user is None:
                continue
            report.append({'user': user.username, 'pending': len(mine)})
            if not opts['dry_run']:
                notify_service.pending_reminder(user, mine)
        self.stdout.write(json.dumps({'dry_run': opts['dry_run'], 'reminded': report}, indent=2))
