"""Register a Workflow-enabled module, idempotently.

    python manage.py register_workflow_module \
        --code BUDGET \
        --name "Budget Approval" \
        --business-table budget.budget_request \
        --business-key-column id \
        --flow-table budget.budget_flow \
        --flow-model budget.BudgetFlow

Re-running with the same `--code` updates the existing row rather than
creating a second one, so it is safe in a deploy script.

`--list` prints the current registry, which is the quickest way to check a
module actually landed.
"""
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from workflow.models import WorkflowModule
from workflow.registry import register_module


class Command(BaseCommand):
    help = 'Register (or update) a module in workflow.workflow_modules.'

    def add_arguments(self, parser):
        parser.add_argument('--code')
        parser.add_argument('--name')
        parser.add_argument('--business-table')
        parser.add_argument('--business-key-column', default='id')
        parser.add_argument('--flow-table')
        parser.add_argument('--flow-model')
        parser.add_argument('--list', action='store_true',
                            help='Print the registry and exit.')

    def handle(self, *args, **opts):
        if opts['list']:
            rows = WorkflowModule.objects.order_by('id')
            if not rows:
                self.stdout.write('workflow.workflow_modules is empty.')
                return
            for m in rows:
                self.stdout.write(
                    f'{m.pk:>4}  {m.code:<14} {m.name}\n'
                    f'      business: {m.business_table} (key={m.business_key_column})\n'
                    f'      flow    : {m.flow_table} -> {m.flow_model}')
            return

        required = ['code', 'name', 'business_table', 'flow_table', 'flow_model']
        missing = [f'--{r.replace("_", "-")}' for r in required if not opts.get(r)]
        if missing:
            raise CommandError(f'missing required options: {", ".join(missing)}')

        try:
            module, created = register_module(
                code=opts['code'],
                name=opts['name'],
                business_table=opts['business_table'],
                business_key_column=opts['business_key_column'],
                flow_table=opts['flow_table'],
                flow_model=opts['flow_model'],
            )
        except ValidationError as exc:
            raise CommandError('; '.join(exc.messages))

        self.stdout.write(self.style.SUCCESS(
            f'{"Registered" if created else "Updated"} module '
            f'{module.code} (id={module.pk})'))
        self.stdout.write(
            f'  business_table      : {module.business_table}\n'
            f'  business_key_column : {module.business_key_column}\n'
            f'  flow_table          : {module.flow_table}\n'
            f'  flow_model          : {module.flow_model}')
