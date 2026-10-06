"""Write the budget hierarchy into the Workflows page: one workflow per head per company.

    python manage.py seed_budget_workflows            # show the plan, write nothing
    python manage.py seed_budget_workflows --apply    # write it
    python manage.py seed_budget_workflows --user "Gurpreet Vg=gurpreet" --user avtar=Avtarsingh

`--user HIERARCHY_NAME=USERNAME` names who holds a hierarchy login on THIS
server: the hierarchy is written with the test server's usernames, and the
live server's differ for some people. Repeatable.

The routes are `budget.hierarchy.routes()`. Idempotent: a workflow is found by
its code and updated in place (stages by sequence), its query rewritten and
validated. Nothing is deleted. The BUDGET module must already be registered,
and the `budget` schema migrated (the queries read `budget.budget_item`).
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from workflow.models import Workflow, WorkflowModule, WorkflowQuery, WorkflowStage
from workflow.services.conditions import validate_and_stamp

from budget import hierarchy
from budget.services.flow import MODULE_CODE


class Command(BaseCommand):
    help = 'Create or update the BUDGET workflows of the hierarchy, one per budget head per company.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Write the workflows (default: show the plan).')
        parser.add_argument('--user', action='append', default=[], metavar='NAME=USERNAME',
                            help='Who a hierarchy username is on this server, e.g. --user avtar=Avtarsingh.')

    def handle(self, *args, **opts):
        routes = hierarchy.routes()
        names = sorted({u for r in routes for _s, u in r['stages']})
        renamed = {}
        for pair in opts['user']:
            name, _sep, username = pair.partition('=')
            if name not in names or not username:
                raise CommandError(f'--user {pair!r}: expected NAME=USERNAME with NAME one of {", ".join(names)}.')
            renamed[name] = username
        login = {n: renamed.get(n, n) for n in names}
        found = {u.username: u for u in get_user_model().objects.filter(username__in=set(login.values()),
                                                                        is_active=True)}
        missing = [f'{n}' + (f' (as {login[n]})' if login[n] != n else '') for n in names if login[n] not in found]
        if missing:
            raise CommandError(f'No active user for: {", ".join(missing)}. '
                               f'Create them, or name another with --user NAME=USERNAME.')
        users = {n: found[login[n]] for n in names}
        for r in routes:
            chain = ' -> '.join(login[u] for _s, u in r['stages'])
            self.stdout.write(f'{r["code"]:<34} [{r["company"]:<9}] {chain}')
        if not opts['apply']:
            self.stdout.write(self.style.WARNING(f'\n{len(routes)} workflows. Dry run: nothing written.'))
            return
        module = WorkflowModule.objects.filter(code=MODULE_CODE).first()
        if module is None:
            raise CommandError(f'Module {MODULE_CODE} is not registered. Register it first.')
        with transaction.atomic():
            for r in routes:
                workflow, _ = Workflow.objects.update_or_create(
                    module=module, code=r['code'],
                    defaults={'name': r['name'], 'company': r['company'], 'is_active': True})
                for sequence, (name, username) in enumerate(r['stages'], start=1):
                    WorkflowStage.objects.update_or_create(
                        workflow=workflow, sequence=sequence,
                        defaults={'name': name, 'user': users[username], 'is_active': True})
                WorkflowStage.objects.filter(workflow=workflow, sequence__gt=len(r['stages'])).update(is_active=False)
                query, _ = WorkflowQuery.objects.update_or_create(
                    workflow=workflow, name=f'{r["code"]}_QUERY',
                    defaults={'company': r['company'], 'query_text': r['query'], 'is_active': True})
                WorkflowQuery.objects.filter(workflow=workflow).exclude(pk=query.pk).update(is_active=False)
                problems = validate_and_stamp(query)
                if problems:
                    self.stdout.write(self.style.ERROR(f'NOT VALIDATED {r["code"]}: {problems}'))
        self.stdout.write(self.style.SUCCESS(f'\n{len(routes)} workflows written.'))
