"""Create the two PRDO workflows, their routing conditions and their stages.

    manage.py seed_prdo_workflows --fg-approver preshit --pm-approver shahrukh
    manage.py seed_prdo_workflows --fg-approver preshit --pm-approver shahrukh --dry-run

WHAT IT CREATES
---------------
An exact translation of the two templates JSAP actually runs (229 and 435),
company OIL only:

    PRDO_OIL_FG   item_code NOT LIKE 'PM%'   finished goods
    PRDO_OIL_PM   item_code     LIKE 'PM%'   packaging material

Both single-stage, because every live JSAP production stage is one user and
one approval. The engine supports more; adding a second stage is an admin
action on the Workflows page, not a code change.

WHY THE APPROVERS ARE REQUIRED ARGUMENTS
-----------------------------------------
JSAP routes FG to Preshit and PM to Shahrukh, but those are JSAP user ids in a
different identity namespace. Guessing at the matching OMS usernames would
create a workflow that routes to the wrong person and looks configured. So the
command asks, and refuses to run without an answer.

WHY THE PAIR MUST BE EXHAUSTIVE
--------------------------------
The engine does NO tie-breaking: zero matches is `WorkflowNotConfigured` and
two matches is `AmbiguousWorkflowSelection`. `LIKE 'PM%'` and `NOT LIKE 'PM%'`
partition every OIL order exactly once, which is what makes that safe. If you
edit these conditions, keep that property or orders will start failing to
route — visibly, at least: the sync reports every unroutable order and exits
non-zero.

BEVERAGES AND MART ARE DELIBERATELY NOT SEEDED
-----------------------------------------------
JSAP's Beverages templates (230, 436) are inactive, its planned-order table
holds zero Beverages rows, and neither company's `SBO_SP_TRANSACTIONNOTIFICATION`
gates production at all. Adding one later is a row on the Workflows page.

Idempotent — re-running updates in place rather than duplicating.
"""
import sys

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from core.companies import OIL
from workflow.models import Workflow, WorkflowQuery, WorkflowStage
from workflow.registry import register_module
from workflow.services import conditions

User = get_user_model()

MODULE_CODE = 'PRDO'
MODULE_NAME = 'Production Order'

#: The OMS table a routing condition reads — the same relation the sync
#: writes. Per BKDT §12: the inner query must expose the key column and must
#: NOT filter by it; the engine binds the document id on an outer wrapper.
DOC_TABLE = 'production.production_order'

#: `(code, name, stage name, condition, which approver)`.
#:
#: The conditions are the exact OMS translations of JSAP queries 231 and 435.
#: `%` is written normally — the engine escapes literal percent signs for
#: psycopg2 itself, so `%%` in stored SQL is wrong (BKDT §12.4).
WORKFLOWS = [
    {
        'code': 'PRDO_OIL_FG',
        'name': 'Production Order — OIL finished goods',
        'stage': 'Sales approval',
        'query_name': 'OIL finished goods',
        'condition': (f"SELECT id FROM {DOC_TABLE}\n"
                      f"WHERE company = 'OIL' AND item_code NOT LIKE 'PM%'"),
        'approver': 'fg_approver',
    },
    {
        'code': 'PRDO_OIL_PM',
        'name': 'Production Order — OIL packaging material',
        'stage': 'Store approval',
        'query_name': 'OIL packaging material',
        'condition': (f"SELECT id FROM {DOC_TABLE}\n"
                      f"WHERE company = 'OIL' AND item_code LIKE 'PM%'"),
        'approver': 'pm_approver',
    },
]


class Command(BaseCommand):
    help = ('Create/update the PRDO workflows, routing conditions and stages '
            'for OIL. Idempotent.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--fg-approver', required=True,
            help='OMS username who approves finished-goods production orders '
                 '(JSAP routes these to Preshit).')
        parser.add_argument(
            '--pm-approver', required=True,
            help='OMS username who approves packaging-material production '
                 'orders (JSAP routes these to Shahrukh).')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change and roll back.')
        parser.add_argument(
            '--deactivate', action='store_true',
            help='Mark both workflows inactive instead of creating them. '
                 'Flows already running are unaffected — selection stops, '
                 'stage activity does not.')

    # -- helpers ---------------------------------------------------------

    def _user(self, username, label):
        user = User.objects.filter(username__iexact=username.strip()).first()
        if not user:
            raise SystemExit(
                self.style.ERROR(
                    f'No OMS user named {username!r} for --{label}. '
                    f'A stage must name a real user, or nothing routes to it.'))
        return user

    def _require_tables(self):
        """Refuse to seed before `migrate` has created the production schema.

        Without this the failure surfaces as the engine's generic "PostgreSQL
        could not plan this query" — true, but it sends you looking at the SQL
        instead of at the migration you have not run. The condition validator
        EXPLAINs against the real relation, so the table must exist first.
        """
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT COUNT(*) FROM information_schema.tables
                   WHERE table_schema = 'production'
                     AND table_name = 'production_order'""")
            if cursor.fetchone()[0]:
                return
        raise SystemExit(self.style.ERROR(
            'The `production` schema does not exist yet, so the routing '
            'conditions cannot be validated against it. '
            'Run `manage.py migrate production` first, then seed.'))

    def _warn_missing_key(self, user):
        """A stage user without the approval key can be assigned but cannot act.

        Worth saying out loud at seed time rather than leaving someone to
        discover it when the first order arrives and nobody can clear it.
        """
        from production import permissions as prdo_perms

        if not prdo_perms.has_approval_access(user):
            self.stderr.write(self.style.WARNING(
                f'  NOTE: {user.username} does not hold '
                f'"{prdo_perms.APPROVAL_KEY}". They will appear as the stage '
                f'user but every decision will be refused until the key is '
                f'granted — both conditions are required.'))

    # -- entry point -----------------------------------------------------

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        if options['deactivate']:
            return self._deactivate(dry_run)

        self._require_tables()
        approvers = {
            'fg_approver': self._user(options['fg_approver'], 'fg-approver'),
            'pm_approver': self._user(options['pm_approver'], 'pm-approver'),
        }

        with transaction.atomic():
            module, created = register_module(code=MODULE_CODE, name=MODULE_NAME)
            self.stdout.write(
                f'module {module.code}: {"created" if created else "already registered"}')

            problems = []
            for spec in WORKFLOWS:
                problems += self._seed_one(spec, approvers[spec['approver']])

            if problems:
                # A workflow whose condition does not validate is worse than no
                # workflow: `validated_at` stays NULL, selection skips the
                # query, and every order fails to route with a message about
                # configuration rather than about the real cause. Roll back.
                self.stderr.write(self.style.ERROR(
                    '\nNot saved — a routing condition failed validation:'))
                for line in problems:
                    self.stderr.write(self.style.ERROR(f'  {line}'))
                transaction.set_rollback(True)
                sys.exit(1)

            if dry_run:
                self.stdout.write(self.style.WARNING(
                    '\n--dry-run: rolling back.'))
                transaction.set_rollback(True)
                return

        self.stdout.write(self.style.SUCCESS('\nPRDO workflows configured.'))
        self.stdout.write(
            'Next: run `manage.py sync_production_orders --dry-run` to confirm '
            'SAP is reachable and every planned order routes to exactly one '
            'of them, then schedule run_production_sync.bat.')

    # -- the work --------------------------------------------------------

    def _seed_one(self, spec, approver):
        module = register_module(code=MODULE_CODE, name=MODULE_NAME)[0]

        workflow, created = Workflow.objects.update_or_create(
            module=module, code=spec['code'],
            defaults={'name': spec['name'], 'company': OIL, 'is_active': True},
        )
        self.stdout.write(
            f'\n{workflow.code}  ({"created" if created else "updated"})')

        query, q_created = WorkflowQuery.objects.update_or_create(
            workflow=workflow, name=spec['query_name'],
            defaults={'company': OIL, 'query_text': spec['condition'],
                      'is_active': True},
        )
        # Validation is what stamps `validated_at`, and selection refuses a
        # query without it. Never write that column by hand.
        problems = conditions.validate_and_stamp(query)
        if problems:
            return [f'{workflow.code} / {query.name}: {p}' for p in problems]

        self.stdout.write(
            f'  condition  {"created" if q_created else "updated"} and validated')
        self.stdout.write(f'             {spec["condition"].splitlines()[-1].strip()}')

        stage, s_created = WorkflowStage.objects.update_or_create(
            workflow=workflow, sequence=1,
            defaults={'name': spec['stage'], 'user': approver, 'is_active': True},
        )
        self.stdout.write(
            f'  stage 1    {"created" if s_created else "updated"} — '
            f'{stage.name} -> {approver.username}')
        self._warn_missing_key(approver)

        # Any stage beyond the first would have been configured by hand; say
        # so rather than silently leaving it out of this report.
        extra = (WorkflowStage.objects
                 .filter(workflow=workflow).exclude(sequence=1)
                 .order_by('sequence'))
        for other in extra:
            self.stdout.write(
                f'  stage {other.sequence}    (existing, left alone) — '
                f'{other.name} -> {getattr(other.user, "username", "?")}')
        return []

    def _deactivate(self, dry_run):
        with transaction.atomic():
            n = (Workflow.objects
                 .filter(module__code=MODULE_CODE,
                         code__in=[w['code'] for w in WORKFLOWS])
                 .update(is_active=False))
            self.stdout.write(f'{n} workflow(s) marked inactive.')
            self.stdout.write(
                'Flows already running are unaffected: selection stops, but '
                '`stages_for` filters on stage activity, not workflow '
                'activity, so nothing in flight deadlocks.')
            if dry_run:
                self.stdout.write(self.style.WARNING('--dry-run: rolling back.'))
                transaction.set_rollback(True)
