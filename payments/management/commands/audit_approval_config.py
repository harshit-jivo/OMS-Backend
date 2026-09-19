"""Is the Workflow Engine configured to route payments today?

Read-only. Run it after any change to payments workflow configuration, and
before a release that depends on one:

    python manage.py audit_approval_config

It answers four questions:

  1. can receipt/deposit numbers serve as the `doc_no` selection key;
  2. is the engine able to route BOTH payments workflows — one module, a
     receipt workflow and a deposit workflow, each with a query that projects
     `doc_no` and reads only its own table;
  3. what state are payments documents actually in;
  4. is any document mid-approval with no flow, which would strand it.

It used to audit the OLD `approvals` engine as well — whether its levels could
be expressed as one-user-per-stage, and what was still in flight. That engine
and its tables have been removed, so those sections went with them.

SAFETY
------
Strictly SELECT-only: the whole audit runs inside one transaction that the
database itself marks READ ONLY, so a write would be refused by PostgreSQL
rather than merely avoided by this code. It contacts no external service.

It also REFUSES TO RUN against production. There is no flag that overrides
that, and `--allow-environment` only names a non-production environment whose
label this command does not recognise.
"""
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Count, Q

from payments.models import BankDeposit, PaymentReceipt

#: Environment labels that are definitely NOT production. Anything else — a
#: label this command does not know, or no label at all — is treated as
#: production and refused, so an unlabelled environment fails closed.
NON_PRODUCTION_ENVIRONMENTS = {
    'development', 'dev', 'local', 'test', 'testing', 'sandbox', 'staging',
}

#: `core.models.next_document_number` is called with these prefixes by the
#: payments serializers; the migration keys workflow selection on them.
RECEIPT_PREFIX = 'RCP-'
DEPOSIT_PREFIX = 'DEP-'


class Command(BaseCommand):
    help = ('Read-only audit: is the Workflow Engine configured to route '
            'payments receipts and deposits? TEST environments only.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--allow-environment', default='',
            help=('Name the non-production environment being audited, when '
                  'its label is one this command does not recognise. It must '
                  'match the detected label exactly, and it can never allow '
                  'production.'))

    # -- environment guard ------------------------------------------------
    def _environment(self):
        """(label, is_production) from the project's own settings.

        `SENTRY_ENVIRONMENT` is the existing indicator (settings.py). It
        defaults to 'production' whenever DEBUG is off, which is exactly the
        fail-closed behaviour this audit wants: an environment that has not
        said what it is counts as production.
        """
        label = str(getattr(settings, 'SENTRY_ENVIRONMENT', '') or '').strip()
        return label, label.lower() not in NON_PRODUCTION_ENVIRONMENTS

    def _check_environment(self, allow):
        label, looks_production = self._environment()
        shown = label or '(unset)'
        if not looks_production:
            return shown
        if allow and allow.strip().lower() == label.lower() \
                and label.lower() != 'production':
            # An unrecognised but explicitly named non-production label.
            return shown
        raise CommandError(
            f'Refusing to run: this environment reports itself as "{shown}". '
            f'This audit is for TEST environments only. If that label names a '
            f'non-production environment, re-run with '
            f'--allow-environment "{shown}". Production can never be audited '
            f'with this command.')

    # -- output helpers ---------------------------------------------------
    def _heading(self, text):
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(text))

    def _line(self, text=''):
        self.stdout.write(text)

    # -- the audit --------------------------------------------------------
    def handle(self, *args, **options):
        label = self._check_environment(options.get('allow_environment') or '')

        db = settings.DATABASES['default']
        self._heading('ENVIRONMENT')
        self._line(f'  Environment : {label}')
        self._line(f'  Engine      : {db.get("ENGINE", "")}')
        self._line(f'  Database    : {db.get("NAME", "")}')
        self._line(f'  Host        : {db.get("HOST", "") or "(local socket)"}')
        self._line(f'  Port        : {db.get("PORT", "")}')
        self._line('  (no credentials are read or printed)')

        # One transaction, marked READ ONLY by the database itself. Every
        # statement below is a SELECT; this makes that a guarantee rather than
        # a promise, and the rollback at the end leaves nothing behind.
        with transaction.atomic():
            read_only = self._begin_read_only()
            self._line(f'  Read-only tx: {read_only}')
            try:
                blockers = self._audit()
            finally:
                transaction.set_rollback(True)

        self._heading('RESULT')
        if blockers:
            self._line(self.style.ERROR('  WORKFLOW CONFIGURATION: BLOCKED'))
            for blocker in blockers:
                self._line(self.style.ERROR(f'    - {blocker}'))
            self._line('')
            self._line('  Nothing was changed. Resolve each blocker in the '
                       'workflow configuration, then re-run.')
            raise SystemExit(1)

        self._line(self.style.SUCCESS('  WORKFLOW CONFIGURATION: PASS'))
        self._line('  Both payments workflows are configured and can route '
                   'their documents.')

    def _begin_read_only(self):
        """Ask PostgreSQL to refuse writes for this transaction."""
        if connection.vendor != 'postgresql':
            return f'not applied ({connection.vendor})'
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION READ ONLY')
            cursor.execute('SHOW transaction_read_only')
            return cursor.fetchone()[0]

    def _audit(self):
        blockers = []
        blockers += self._report_document_numbers()
        blockers += self._report_engine_readiness()
        self._report_document_states()
        blockers += self._report_stranded()
        return blockers

    # B. the engine's own configuration -------------------------------
    def _report_engine_readiness(self):
        """Is the Workflow Engine actually able to route payments today?

        The part that decides whether payments can be submitted at all: a
        missing workflow, or a query without the `doc_no` alias, stops
        submission dead — and neither fails until someone tries.
        """
        from workflow.models import (Workflow, WorkflowModule, WorkflowQuery,
                                     WorkflowStage)

        from payments.apps import MODULE_CODE
        from payments.workflow_flow import DocumentKind, kinds_a_query_serves

        blockers = []
        self._heading('B. WORKFLOW ENGINE CONFIGURATION')

        modules = list(WorkflowModule.objects.filter(code=MODULE_CODE))
        self._line(f'  module rows for {MODULE_CODE}      : {len(modules)}')
        for module in modules:
            self._line(f'    id={module.id} code={module.code} '
                       f'name={module.name}')
        if not modules:
            blockers.append(
                f'No {MODULE_CODE} module is registered in the Workflow '
                f'Engine. It is created by post_migrate; run migrate.')
        elif len(modules) > 1:
            blockers.append(
                f'{len(modules)} {MODULE_CODE} module rows exist. Payments '
                f'must register exactly ONE.')

        workflows = list(
            Workflow.objects
            .filter(module__code=MODULE_CODE)
            .order_by('-is_active', 'company', 'code'))
        active = [w for w in workflows if w.is_active]
        self._line(f'  workflows (active/total)   : {len(active)}/'
                   f'{len(workflows)}')
        if not active:
            blockers.append(
                'No ACTIVE payments workflow exists in the Workflow Engine. '
                'Every submission would be refused with "No workflow is '
                'configured for this document".')

        # The SAME definition routing uses (`workflow_flow.kinds_a_query_serves`),
        # so this audit cannot drift from the rule actually enforced at submit.
        serves = {DocumentKind.RECEIPT: 0, DocumentKind.DEPOSIT: 0}

        for workflow in workflows:
            queries = list(WorkflowQuery.objects.filter(workflow=workflow))
            stages = list(WorkflowStage.objects
                          .filter(workflow=workflow)
                          .order_by('sequence'))
            flag = 'yes' if workflow.is_active else 'no '
            scope = workflow.company or 'ALL'
            self._line('')
            self._line(f'  [{flag}] {workflow.code} company={scope} '
                       f'stages={len(stages)} queries={len(queries)}')

            for query in queries:
                lowered = (query.query_text or '').lower()
                has_alias = 'as doc_no' in lowered.replace('  ', ' ')
                targets = sorted(kinds_a_query_serves(query.query_text))
                if workflow.is_active:
                    for target in targets:
                        serves[target] += 1
                mark = 'doc_no OK ' if has_alias else 'NO doc_no'
                self._line(f'      - {mark} target={"/".join(targets) or "?"} '
                           f'name={query.name}')
                if workflow.is_active and not has_alias:
                    blockers.append(
                        f'Query "{query.name}" on active workflow '
                        f'{workflow.code} does not project `doc_no`. Every '
                        f'submission it routes fails with "column wf_q.doc_no '
                        f'does not exist".')
                if workflow.is_active and not targets:
                    blockers.append(
                        f'Query "{query.name}" on active workflow '
                        f'{workflow.code} reads neither the receipt nor the '
                        f'deposit table, so nothing it matches can be routed.')
                if workflow.is_active and len(targets) > 1:
                    blockers.append(
                        f'Query "{query.name}" on active workflow '
                        f'{workflow.code} reads BOTH the receipt and the '
                        f'deposit tables. Receipts and deposits are separate '
                        f'business workflows and must be routed by separate '
                        f'queries; submission through this query is refused.')

            # One user per stage is the engine's shape; report the actual
            # assignment so a stage with nobody on it is visible here rather
            # than at submit time.
            for stage in stages:
                who = getattr(stage.user, 'username', None) or '(unassigned)'
                state = 'active' if stage.is_active else 'INACTIVE'
                self._line(f'      #{stage.sequence} {state} user={who}')
                if workflow.is_active and stage.is_active and not stage.user_id:
                    blockers.append(
                        f'Stage {stage.sequence} of {workflow.code} has no '
                        f'user assigned, so nothing can be approved there.')
            if workflow.is_active and not stages:
                blockers.append(
                    f'Active workflow {workflow.code} has no stages, so a '
                    f'document routed to it can never be approved.')

        self._line('')
        for kind in (DocumentKind.RECEIPT, DocumentKind.DEPOSIT):
            self._line(f'  active queries serving {kind:<8}: {serves[kind]}')
        for kind in (DocumentKind.RECEIPT, DocumentKind.DEPOSIT):
            if not serves[kind]:
                blockers.append(
                    f'No active payments query targets {kind} documents. '
                    f'{kind.title()}s cannot be submitted at all.')
        return blockers

    # C. document states ---------------------------------------------------
    def _report_document_states(self):
        from django.db.models import Count as _Count

        from payments.models import BankDeposit, PaymentReceipt

        self._heading('C. PAYMENTS DOCUMENT STATES')
        for label, model in (('receipts', PaymentReceipt),
                             ('deposits', BankDeposit)):
            rows = (model.objects.values('status')
                    .annotate(n=_Count('id')).order_by('status'))
            counts = {r['status']: r['n'] for r in rows}
            self._line(f'  {label}:')
            if not counts:
                self._line('    (none)')
            for status, n in sorted(counts.items()):
                self._line(f'    {status:<20} {n}')

    # D. documents that would strand ---------------------------------------
    def _report_stranded(self):
        """Documents the new engine could not act on, and why it matters.

        A document mid-approval in the OLD engine has no flow row. Once the
        new engine is serving, it can be neither approved (there is no flow)
        nor resubmitted (its status forbids it) — it is stuck until someone
        changes it by hand. This is the single most important number to drive
        to zero before a cutover.
        """
        from payments.models import (BankDeposit, BankDepositFlow,
                                     PaymentReceipt, PaymentReceiptFlow)

        blockers = []
        self._heading('D. DOCUMENTS MID-APPROVAL WITH NO FLOW')

        for label, model, flow_model, column in (
            ('receipts', PaymentReceipt, PaymentReceiptFlow, 'receipt_id'),
            ('deposits', BankDeposit, BankDepositFlow, 'deposit_id'),
        ):
            with_flow = set(flow_model.objects.values_list(column, flat=True))
            mid_approval = (model.objects
                            .filter(status=model.Status.PENDING_APPROVAL)
                            .exclude(pk__in=with_flow)
                            .order_by('created_at'))
            total = mid_approval.count()
            self._line(f'  {label} PENDING_APPROVAL with no engine flow: '
                       f'{total}')
            for document in mid_approval[:20]:
                number = (getattr(document, 'receipt_no', None)
                          or document.deposit_no)
                self._line(f'    {document.pk:>6}  {number}  '
                           f'{document.company}')
            if total > 20:
                self._line(f'    ... and {total - 20} more')
            if total:
                self._line('    ^ these must be decided through the '
                           'application BEFORE the cutover, or they stick.')

        self._line('')
        self._line('  These are REPORTED ONLY. Nothing was changed.')
        return blockers

    # A. workflows ---------------------------------------------------------
    def _report_document_numbers(self):
        """Receipt and deposit numbers become the `doc_no` selection key.

        Payments registers ONE module, so both document types are selected
        through one key column. That only works if the values cannot collide
        and each type is recognisable, which is what this checks against the
        real rows.
        """
        self._heading('A. DOCUMENT NUMBERS AS THE doc_no SELECTION KEY')
        blockers = []

        receipts = PaymentReceipt.objects.count()
        deposits = BankDeposit.objects.count()
        receipt_numbers = set(
            PaymentReceipt.objects.values_list('receipt_no', flat=True))
        deposit_numbers = set(
            BankDeposit.objects.values_list('deposit_no', flat=True))

        receipt_dupes = receipts - len(receipt_numbers)
        deposit_dupes = deposits - len(deposit_numbers)
        collisions = receipt_numbers & deposit_numbers
        bad_receipts = [n for n in receipt_numbers
                        if not (n or '').startswith(RECEIPT_PREFIX)]
        bad_deposits = [n for n in deposit_numbers
                        if not (n or '').startswith(DEPOSIT_PREFIX)]

        declared_receipt_unique = PaymentReceipt._meta.get_field(
            'receipt_no').unique
        declared_deposit_unique = BankDeposit._meta.get_field(
            'deposit_no').unique

        self._line(f'  receipts                     : {receipts}')
        self._line(f'  deposits                     : {deposits}')
        self._line('  receipt_no unique (declared) : {}'.format(
            declared_receipt_unique))
        self._line('  deposit_no unique (declared) : {}'.format(
            declared_deposit_unique))
        self._line(f'  duplicate receipt numbers    : {receipt_dupes}')
        self._line(f'  duplicate deposit numbers    : {deposit_dupes}')
        self._line('  receipts not "{}"-prefixed   : {}'.format(
            RECEIPT_PREFIX.rstrip("-"), len(bad_receipts)))
        self._line('  deposits not "{}"-prefixed   : {}'.format(
            DEPOSIT_PREFIX.rstrip("-"), len(bad_deposits)))
        self._line(f'  receipt/deposit collisions   : {len(collisions)}')

        for title, offenders in (('receipt numbers', bad_receipts),
                                 ('deposit numbers', bad_deposits),
                                 ('colliding numbers', sorted(collisions))):
            if offenders:
                self._line('')
                self._line(self.style.WARNING(f'  unexpected {title}:'))
                for value in sorted(offenders)[:20]:
                    self._line(f'    {value}')
                if len(offenders) > 20:
                    self._line(f'    … and {len(offenders) - 20} more')

        if receipt_dupes or deposit_dupes:
            blockers.append('document numbers are not unique')
        if collisions:
            blockers.append(
                f'{len(collisions)} document number(s) exist as BOTH a receipt '
                f'and a deposit')
        if bad_receipts or bad_deposits:
            blockers.append(
                'some document numbers do not carry the expected prefix, so '
                'the two types are not distinguishable by number alone')
        if not blockers:
            self._line('')
            self._line('  doc_no is usable: unique within each type, '
                       'prefix-distinguishable, no collisions.')
        return blockers
