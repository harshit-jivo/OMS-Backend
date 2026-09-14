"""Register a Workflow-enabled module, idempotently.

    python manage.py register_workflow_module \
        --code BUDGET \
        --name "Budget Approval"

`code` and `name` are the whole registration. There are deliberately no
options for the module's tables or flow model: `workflow_modules` holds
identity only, and the module's own models are the source of truth for the
rest (see `workflow/registry.py`).

Re-running with the same `--code` updates the existing row rather than
creating a second one, so it is safe in a deploy script:

    first  -> Registered module BUDGET (id=20)
    second -> Updated module BUDGET (id=20)      # still one row

Normally a module registers itself at deploy time from its own AppConfig, and
this command is the manual escape hatch. `--list` prints the current registry,
which is the quickest way to check a module actually landed.
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
                    f'{m.pk:>4}  {m.code:<14} {m.name}'
                    f'   workflows={m.workflows.count()}')
            return

        missing = [f'--{r}' for r in ('code', 'name') if not opts.get(r)]
        if missing:
            raise CommandError(f'missing required options: {", ".join(missing)}')

        try:
            module, created = register_module(
                code=opts['code'], name=opts['name'],
            )
        except ValidationError as exc:
            raise CommandError('; '.join(exc.messages))

        self.stdout.write(self.style.SUCCESS(
            f'{"Registered" if created else "Updated"} module '
            f'{module.code} (id={module.pk})'))
        self.stdout.write(f'  code : {module.code}\n'
                          f'  name : {module.name}')
