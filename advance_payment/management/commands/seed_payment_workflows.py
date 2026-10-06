"""Write the approval hierarchy into the Workflows page: one workflow per purpose.

    python manage.py seed_payment_workflows                    # show the plan, write nothing
    python manage.py seed_payment_workflows --apply            # write it
    python manage.py seed_payment_workflows --apply --retire ADV_IT_SW,ADV_HR
    python manage.py seed_payment_workflows --user director="Gurpreet Vg"

The routes are `advance_payment.hierarchy.routes()`; the people are
`hierarchy.PEOPLE`, overridable with `--user role=username`. Idempotent: a
workflow is found by its code and updated in place (stages matched by their
sequence, so the flows already on them keep pointing at the same rows), its
query rewritten and validated. Nothing is deleted. `--retire` deactivates
other ADVANCE_PAYMENT workflows by code, which a catch-all query left over from
testing must be, or every request would match two workflows.

The ADVANCE_PAYMENT module must already be registered (the Workflows page,
or `register_workflow_module`); this command does not register it.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from workflow.models import Workflow, WorkflowModule, WorkflowQuery, WorkflowStage
from workflow.services.conditions import validate_and_stamp

from advance_payment import hierarchy
from advance_payment.services.flow import MODULE_CODE


class Command(BaseCommand):
    help = 'Create or update the ADVANCE_PAYMENT workflows of the approval hierarchy, one per purpose.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Write the workflows (default: show the plan).')
        parser.add_argument('--retire', default='',
                            help='Comma-separated codes of other ADVANCE_PAYMENT workflows to deactivate.')
        parser.add_argument('--user', action='append', default=[], metavar='ROLE=USERNAME',
                            help='Who holds a role, e.g. --user payment=taran. Repeatable.')

    def handle(self, *args, **opts):
        people = dict(hierarchy.PEOPLE)
        for pair in opts['user']:
            role, _sep, username = pair.partition('=')
            if role not in people or not username:
                raise CommandError(f'--user {pair!r}: expected ROLE=USERNAME with ROLE one of {", ".join(people)}.')
            people[role] = username
        users = self._users(people)
        module = WorkflowModule.objects.filter(code=MODULE_CODE).first()
        if module is None:
            raise CommandError(f'Module {MODULE_CODE} is not registered. Register it first.')

        routes = hierarchy.routes()
        retire = [c.strip() for c in opts['retire'].split(',') if c.strip()]
        self._plan(routes, people, retire)
        if not opts['apply']:
            self.stdout.write(self.style.WARNING('\nDry run: nothing written. Re-run with --apply.'))
            return

        with transaction.atomic():
            for route in routes:
                self._write(module, route, users)
            for code in retire:
                n = Workflow.objects.filter(module=module, code=code).update(is_active=False)
                self.stdout.write(f'retired {code}' if n else self.style.WARNING(f'no workflow {code} to retire'))
        bad = WorkflowQuery.objects.filter(workflow__module=module, workflow__code__in=[r['code'] for r in routes],
                                           validated_at__isnull=True)
        for q in bad:
            self.stdout.write(self.style.ERROR(f'NOT VALIDATED {q.workflow.code}: {q.validation_error}'))
        self.stdout.write(self.style.SUCCESS(f'\n{len(routes)} workflows written.'))

    def _users(self, people):
        User = get_user_model()
        found, missing = {}, []
        for role, username in people.items():
            user = User.objects.filter(username=username, is_active=True).first()
            if user is None:
                missing.append(f'{role}={username}')
            found[role] = user
        if missing:
            raise CommandError('No active user for: ' + ', '.join(missing)
                               + '. Create them, or name another with --user ROLE=USERNAME.')
        return found

    def _plan(self, routes, people, retire):
        for route in routes:
            chain = ' -> '.join('Department Head (picked)' if role == hierarchy.HEAD else f'{people[role]}'
                                for _name, role in hierarchy.full_stages(route))
            self.stdout.write(f'{route["code"]:<26} [{route["company"]:<9}] {chain}')
            self.stdout.write(f'    {route["query"]}')
        if retire:
            self.stdout.write(f'\nretire: {", ".join(retire)}')

    def _write(self, module, route, users):
        workflow, _created = Workflow.objects.update_or_create(
            module=module, code=route['code'],
            defaults={'name': route['name'], 'company': route['company'], 'is_active': True})
        stages = hierarchy.full_stages(route)
        for sequence, (name, role) in enumerate(stages, start=1):
            user = users[hierarchy.HEAD_PLACEHOLDER if role == hierarchy.HEAD else role]
            WorkflowStage.objects.update_or_create(
                workflow=workflow, sequence=sequence,
                defaults={'name': name, 'user': user, 'is_active': True})
        WorkflowStage.objects.filter(workflow=workflow, sequence__gt=len(stages)).update(is_active=False)
        query, _created = WorkflowQuery.objects.update_or_create(
            workflow=workflow, name=f'{route["code"]}_QUERY',
            defaults={'company': route['company'], 'query_text': route['query'], 'is_active': True})
        WorkflowQuery.objects.filter(workflow=workflow).exclude(pk=query.pk).update(is_active=False)
        validate_and_stamp(query)
