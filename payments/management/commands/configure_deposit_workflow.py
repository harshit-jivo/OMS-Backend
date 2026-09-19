"""Create the DEPOSIT workflow in the Workflow Engine.

    python manage.py configure_deposit_workflow --approver <username> [--dry-run]

WHY THIS IS A COMMAND AND NOT A MIGRATION
-----------------------------------------
Workflows, queries, stages and approver assignments are CONFIGURATION — an
administrator writes them on the Workflows page — and this project keeps them
that way: `backdate/migrations/0007_repoint_condition_queries.py` REPAIRS such
rows when code forces it, but nothing creates them. A migration that invented
an approver would be deciding, silently and permanently, who may release money.

So this exists instead: the same work the Workflows page does, done once,
reviewably, with the approver named on the command line rather than guessed.
It is safe to run in production when the time comes, and refuses to run twice.

WHY NOT RAW SQL
---------------
`workflow_queries.validated_at` is stamped by the engine's own validator, and
selection REFUSES a query whose stamp is NULL. A hand-written INSERT therefore
produces configuration that silently never routes anything. This builds the
query through `conditions.validate_and_stamp`, exactly as the page does.

WHAT IT CREATES
---------------
One workflow, one query and one stage per approver:

    workflow  DEPOSIT_APPROVAL         (--code to override)
    query     SELECT *, deposit_no AS doc_no FROM <deposit table>
    stage(s)  one per --approver, in the order given

The query reads the DEPOSIT table and nothing else, which is what makes it a
deposit workflow: `payments.workflow_flow` routes on the table a query reads,
so a receipt can never be captured by this workflow. See `_guard_routing`.

The leading `SELECT *` is required, not decoration — configuration-time
validation checks the engine's default key `id`, and a projection naming only
`doc_no` would be refused before it could be stamped.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from workflow.models import (COMPANY_ALL, COMPANY_VALUES, Workflow,
                             WorkflowQuery, WorkflowStage)
from workflow.services import conditions

from payments.apps import MODULE_CODE
from payments.models import BankDeposit
from payments.permissions import DEPOSIT_APPROVE, granted_keys
from payments.workflow_flow import DocumentKind, kinds_a_query_serves

DEFAULT_CODE = 'DEPOSIT_APPROVAL'
DEFAULT_NAME = 'Deposit approval'


class Command(BaseCommand):
    help = ('Create the DEPOSIT workflow (workflow + query + stages) in the '
            'Workflow Engine. Names its approvers explicitly; refuses to '
            'create a second one.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--approver', action='append', default=[], metavar='USERNAME',
            help=('Username of a stage approver. Repeat for several stages, '
                  'in approval order. Required.'))
        parser.add_argument(
            '--company', default=COMPANY_ALL,
            help=f'Company scope for the workflow. Default {COMPANY_ALL}.')
        parser.add_argument('--code', default=DEFAULT_CODE,
                            help=f'Workflow code. Default {DEFAULT_CODE}.')
        parser.add_argument('--name', default=DEFAULT_NAME,
                            help='Human-readable workflow name.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be created and change nothing.')

    def handle(self, *args, **options):
        approvers = self._resolve_approvers(options['approver'])
        company = self._resolve_company(options['company'])
        code = (options['code'] or DEFAULT_CODE).strip()

        self._refuse_if_already_configured(code)

        plan = [
            f'workflow  {code}  company={company}  name={options["name"]!r}',
            f'query     {self._query_text()}',
        ]
        for index, user in enumerate(approvers, start=1):
            plan.append(f'stage {index}   {user.username} ({user.name})')

        self.stdout.write(self.style.MIGRATE_HEADING('WOULD CREATE'
                                                     if options['dry_run']
                                                     else 'CREATING'))
        for line in plan:
            self.stdout.write(f'  {line}')

        if options['dry_run']:
            self.stdout.write('')
            self.stdout.write('Dry run — nothing was written.')
            return

        with transaction.atomic():
            workflow = self._create(code, options['name'], company, approvers)

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Created workflow {workflow.code} (id={workflow.pk}).'))
        self.stdout.write('Verify with: python manage.py '
                          'audit_approval_config')

    # -- validation --------------------------------------------------------

    def _resolve_approvers(self, usernames):
        """Every stage user, checked for the key they will actually need."""
        from django.contrib.auth import get_user_model

        if not usernames:
            raise CommandError(
                'Name at least one approver: --approver <username>. This is '
                'deliberately not defaulted — who may approve a deposit is a '
                'business decision, not something this command may choose.')

        User = get_user_model()
        resolved = []
        for username in usernames:
            user = User.objects.filter(username=username).first()
            if user is None:
                raise CommandError(f'No user named "{username}".')
            if not user.is_active:
                raise CommandError(
                    f'"{username}" is not an active account.')
            # BOTH gates are required to approve: being the stage's user AND
            # holding the action key (payments.permissions.may_act_on). A
            # stage naming someone without the key is configuration that looks
            # right and approves nothing, so it is refused here rather than
            # discovered by the first person who tries to use it.
            if DEPOSIT_APPROVE not in granted_keys(user):
                raise CommandError(
                    f'"{username}" does not hold the {DEPOSIT_APPROVE} '
                    f'permission, so they could not approve a deposit even as '
                    f'a stage user. Grant it on the Permissions page first.')
            resolved.append(user)
        return resolved

    def _resolve_company(self, company):
        value = (company or COMPANY_ALL).strip().upper()
        if value not in COMPANY_VALUES:
            raise CommandError(
                f'Unknown company "{company}". Use one of: '
                f'{", ".join(COMPANY_VALUES)}.')
        return value

    def _refuse_if_already_configured(self, code):
        """One deposit workflow, or none. Never a second.

        The engine has no tie-break: two active workflows whose queries both
        match a document is `AmbiguousWorkflowSelection`, and every deposit
        submission would fail. Creating a duplicate is therefore not merely
        untidy, it is an outage.
        """
        if Workflow.objects.filter(module__code=MODULE_CODE,
                                   code=code).exists():
            raise CommandError(
                f'A workflow with code "{code}" already exists. Nothing was '
                f'created.')

        serving = [
            query for query in WorkflowQuery.objects.filter(
                workflow__module__code=MODULE_CODE,
                workflow__is_active=True).select_related('workflow')
            if DocumentKind.DEPOSIT in kinds_a_query_serves(query.query_text)
        ]
        if serving:
            existing = ', '.join(
                f'{q.workflow.code}/"{q.name}"' for q in serving)
            raise CommandError(
                f'An active payments workflow already routes deposits '
                f'({existing}). A second one would make every deposit '
                f'submission ambiguous. Nothing was created.')

    # -- creation ----------------------------------------------------------

    def _query_text(self):
        return (f'SELECT *, deposit_no AS doc_no '
                f'FROM {BankDeposit._meta.db_table}')

    def _create(self, code, name, company, approvers):
        from workflow.models import WorkflowModule

        # The module row is created by payments' own `post_migrate` hook.
        # Looking it up rather than creating it keeps this command to
        # CONFIGURATION: if the module is missing, migrations have not run and
        # that is what needs fixing, not a module invented here.
        module = WorkflowModule.objects.filter(code=MODULE_CODE).first()
        if module is None:
            raise CommandError(
                f'The {MODULE_CODE} module is not registered with the '
                f'Workflow Engine. It is created by post_migrate — run '
                f'`python manage.py migrate` first.')

        workflow = Workflow.objects.create(
            module=module, code=code, name=name, company=company,
            is_active=True)

        query = WorkflowQuery.objects.create(
            workflow=workflow, name='All deposits',
            query_text=self._query_text(), company=company)
        problems = conditions.validate_and_stamp(query)
        if problems:
            # Inside the atomic block, so the workflow goes with it: a
            # half-built workflow whose query never validated would route
            # nothing and report nothing.
            raise CommandError(
                'The deposit query failed validation and nothing was saved: '
                + '; '.join(str(p) for p in problems))

        for index, user in enumerate(approvers, start=1):
            WorkflowStage.objects.create(
                workflow=workflow, sequence=index, user=user,
                name=f'Deposit approval {index}', is_active=True)
        return workflow
