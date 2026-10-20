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
from advance_payment.models import ACCOUNTS_STAGE, FlowStatus, RequestFlow
from advance_payment.services import flow as flow_service
from advance_payment.services.flow import MODULE_CODE

#: A stage renamed in place: the row keeps its requests under its new name.
#: An Expense route's Payment desk became the Accounts desk (2026-10-20).
RENAMED = {ACCOUNTS_STAGE: 'Payment Approval'}


class Command(BaseCommand):
    help = 'Create or update the ADVANCE_PAYMENT workflows of the approval hierarchy, one per purpose.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Write the workflows (default: show the plan).')
        parser.add_argument('--retire', default='',
                            help='Comma-separated codes of other ADVANCE_PAYMENT workflows to deactivate.')
        parser.add_argument('--only', default='',
                            help='Comma-separated workflow codes: write only these, leave every other '
                                 'workflow (and its hand-made changes) as it is; retires nothing.')
        parser.add_argument('--user', action='append', default=[], metavar='ROLE=USERNAME',
                            help='Who holds a role, e.g. --user payment=taran. Repeatable.')

    def handle(self, *args, **opts):
        people = dict(hierarchy.PEOPLE)
        for pair in opts['user']:
            role, _sep, username = pair.partition('=')
            if role not in people or not username:
                raise CommandError(f'--user {pair!r}: expected ROLE=USERNAME with ROLE one of {", ".join(people)}.')
            people[role] = username
        module = WorkflowModule.objects.filter(code=MODULE_CODE).first()
        if module is None:
            raise CommandError(f'Module {MODULE_CODE} is not registered. Register it first.')

        routes = hierarchy.routes()
        retire = [c.strip() for c in opts['retire'].split(',') if c.strip()]
        only = {c.strip() for c in opts['only'].split(',') if c.strip()}
        if only:
            unknown = only - {r['code'] for r in routes}
            if unknown:
                raise CommandError(f'--only: no such route {", ".join(sorted(unknown))}.')
            routes = [r for r in routes if r['code'] in only]
            # Only the roles those workflows use need a login here.
            used = {hierarchy.HEAD_PLACEHOLDER if role == hierarchy.HEAD else role
                    for route in routes for _name, role in hierarchy.full_stages(route)}
            people = {role: name for role, name in people.items() if role in used}
        else:
            # Workflows the hierarchy no longer has: deactivated with the rest.
            retire += [c for c in [*hierarchy.retired_expense_routes(), *hierarchy.RETIRED_ROUTES]
                       if c not in retire]
        users = self._users(people)
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
            # The same login may be cased differently on each server ("Parmeet", "parmeet").
            user = (User.objects.filter(username=username, is_active=True).first()
                    or User.objects.filter(username__iexact=username, is_active=True).first())
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

    @staticmethod
    def _write_stages(workflow, stages, users):
        """The workflow's stages, matched to the existing rows by NAME, not sequence.

        A request mid-route points at a stage row: matched by name, a request at
        Audit stays at Audit when a stage is inserted before it (the CA desk,
        2026-10-19), instead of the row being renamed under it. A stage the
        route no longer has is deactivated, never deleted, and numbered BEFORE
        the live stages, so a request still sitting on it moves on to the
        route's first live stage when approved.
        """
        def key(name):
            return ' '.join(name.split()).lower()

        rows = list(WorkflowStage.objects.filter(workflow=workflow).order_by('-is_active', 'sequence', 'id'))
        by_name = {}
        for row in rows:
            by_name.setdefault(key(row.name), row)
        names = {key(name) for name, _role in stages}
        wanted = []
        for name, role in stages:
            user = users[hierarchy.HEAD_PLACEHOLDER if role == hierarchy.HEAD else role]
            # Renamed: the old row, unless the route still has a stage of the old name.
            old = RENAMED.get(name)
            renamed = by_name.pop(key(old), None) if old and key(old) not in names else None
            row = by_name.pop(key(name), None) or renamed or WorkflowStage(workflow=workflow, name=name)
            row.name, row.user, row.is_active = name, user, True
            wanted.append(row)
        kept = {id(r) for r in wanted}
        retired = [r for r in rows if id(r) not in kept]
        # Out of the way first: (workflow, sequence) is unique.
        for i, row in enumerate(rows, start=1000):
            WorkflowStage.objects.filter(pk=row.pk).update(sequence=i)
        retired_ids = {id(r) for r in retired}
        for sequence, row in enumerate([*retired, *wanted], start=1):
            row.sequence = sequence
            if id(row) in retired_ids:
                row.is_active = False
            row.save()

    def _refresh_waiting(self, workflow):
        """Who a waiting request is with, again: a stage's user may have changed
        (the Accounts desk took Expense Payment over, 2026-10-20), and the
        queues list by the flow's copy of it."""
        flows = (RequestFlow.objects.select_related('request', 'current_stage')
                 .filter(workflow=workflow, status=FlowStatus.PENDING, current_stage__isnull=False))
        moved = 0
        for flow in flows:
            user_id = flow_service.stage_user_id(flow.request, flow.current_stage)
            if user_id != flow.current_user_id:
                RequestFlow.objects.filter(pk=flow.pk).update(current_user_id=user_id)
                moved += 1
        if moved:
            self.stdout.write(f"  {workflow.code}: {moved} waiting request(s) now with their stage's user")

    def _write(self, module, route, users):
        workflow, _created = Workflow.objects.update_or_create(
            module=module, code=route['code'],
            defaults={'name': route['name'], 'company': route['company'], 'is_active': True})
        self._write_stages(workflow, hierarchy.full_stages(route), users)
        self._refresh_waiting(workflow)
        query, _created = WorkflowQuery.objects.update_or_create(
            workflow=workflow, name=f'{route["code"]}_QUERY',
            defaults={'company': route['company'], 'query_text': route['query'], 'is_active': True})
        WorkflowQuery.objects.filter(workflow=workflow).exclude(pk=query.pk).update(is_active=False)
        validate_and_stamp(query)
