"""Pull SAP's planned production orders into OMS and route them for approval.

    manage.py sync_production_orders
    manage.py sync_production_orders --company OIL --verbose
    manage.py sync_production_orders --dry-run

Scheduled on .118 via `run_production_sync.bat`, every 15 minutes. The feed it
replaces averaged 3-29 rows a DAY, so JSAP's 60-second cadence bought nothing.

EXIT CODES — THE POINT OF THIS COMMAND
---------------------------------------
    0   ran, and either saw orders or has not been silent for long
    1   SAP unreachable, a row failed, or an order could not be routed
    2   SAP returned nothing AND has returned nothing for longer than
        PRODUCTION_SYNC_SILENCE_HOURS

Code 2 exists because of exactly one incident. JSAP's feed died on
13 Aug 2026; its job kept succeeding every minute for 33 days because "no rows"
and "no connection" looked identical to it, and 432 production orders went
through no approval at all. A sweep that finds nothing for a day is not
healthy, and must not report that it is.

`--dry-run` reports what would change and writes nothing — but still contacts
SAP, so it also serves as a connectivity check.
"""
import sys

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.companies import COMPANY_CODES
from production.services import sap as sap_service
from production.services import sync as sync_service

#: How long SAP may report nothing before a run is treated as suspicious.
#: Overridable in settings; a day is long enough to cover a quiet weekend at
#: this volume and short enough that 33 days could never happen again.
DEFAULT_SILENCE_HOURS = 24


class Command(BaseCommand):
    help = ('Sync planned production orders from SAP and open approval flows. '
            'Retires requests SAP has moved out of Planned.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--company', action='append', choices=list(COMPANY_CODES),
            help='Limit to one company. Repeatable. Default: every company '
                 'that has an active PRDO workflow configured.')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Contact SAP and report, but write nothing.')
        parser.add_argument(
            '--verbose', action='store_true',
            help='List each order that was created, refreshed or retired.')
        parser.add_argument(
            '--silence-hours', type=int, default=None,
            help='Override the silence threshold that triggers exit code 2.')

    # -- helpers ---------------------------------------------------------

    def _companies(self, options):
        if options.get('company'):
            return list(dict.fromkeys(options['company']))
        return self._configured_companies()

    def _configured_companies(self):
        """Companies with at least one active PRDO workflow.

        Derived from configuration rather than hardcoded, so enabling
        Beverages is a workflow row and not a code change. Falls back to the
        full set if the engine has nothing yet, so a first run still reports
        something useful instead of silently doing nothing.
        """
        from workflow.models import Workflow

        codes = set(
            Workflow.objects
            .filter(module__code='PRDO', is_active=True)
            .values_list('company', flat=True)
        )
        if not codes:
            return []
        if 'ALL' in codes:
            return list(COMPANY_CODES)
        return [c for c in COMPANY_CODES if c in codes]

    def _silence_hours(self, options):
        if options.get('silence_hours') is not None:
            return options['silence_hours']
        return getattr(settings, 'PRODUCTION_SYNC_SILENCE_HOURS',
                       DEFAULT_SILENCE_HOURS)

    # -- entry point -----------------------------------------------------

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        verbose = options['verbose']
        started = timezone.now()

        companies = self._companies(options)
        if not companies:
            # Not an error, and not silently fine either: the module is
            # installed but nothing is configured to use it.
            self.stderr.write(self.style.WARNING(
                'No active PRDO workflow is configured for any company, so '
                'there is nothing to sync. Configure one under '
                '/api/workflow/ before scheduling this job.'))
            sys.exit(1)

        self.stdout.write(
            f'[{started:%Y-%m-%d %H:%M:%S}] PRDO sync starting for '
            f'{", ".join(companies)}'
            + ('  (DRY RUN — nothing will be written)' if dry_run else ''))

        results, failed = [], False
        for company in companies:
            try:
                result = sync_service.run(company, dry_run=dry_run)
            except sap_service.SapUnavailable as exc:
                # The distinction that makes this job honest: SAP was not
                # asked successfully, so we know nothing about that company.
                failed = True
                self.stderr.write(self.style.ERROR(
                    f'  {company}: SAP UNREACHABLE — {exc}'))
                continue
            results.append(result)
            self._report(result, verbose=verbose)
            if result.errors or result.unroutable:
                failed = True

        total_seen = sum(r.seen for r in results)
        elapsed = (timezone.now() - started).total_seconds()
        self.stdout.write(
            f'[{timezone.now():%Y-%m-%d %H:%M:%S}] PRDO sync done in '
            f'{elapsed:.1f}s — {total_seen} order(s) seen, '
            f'{sum(r.created for r in results)} new, '
            f'{sum(r.updated for r in results)} refreshed, '
            f'{sum(r.retired for r in results)} retired.')

        if failed:
            sys.exit(1)
        self._check_silence(results, self._silence_hours(options))

    # -- reporting -------------------------------------------------------

    def _report(self, result, *, verbose):
        line = (f'  {result.company}: {result.seen} planned, '
                f'{result.created} new, {result.updated} refreshed, '
                f'{result.retired} retired')
        self.stdout.write(line)

        for row in result.unroutable:
            self.stderr.write(self.style.ERROR(
                f'    NOT ROUTED  DocEntry {row["doc_entry"]} '
                f'({row["item_code"]}): {row["reason"]}'))
        for row in result.errors:
            self.stderr.write(self.style.ERROR(
                f'    FAILED      DocEntry {row["doc_entry"]}: {row["error"]}'))
        # A dry run proves routing by running selection and rolling it back,
        # so this is the configuration as it actually stands.
        for row in result.routed:
            self.stdout.write(
                f'    would route  DocEntry {row["doc_entry"]} '
                f'({row["item_code"]}) -> {row["workflow"]} / '
                f'{row["stage"]} -> {row["user"]}')
        if verbose and result.seen and not result.routed:
            self.stdout.write(f'    (use the API or admin to list the '
                              f'{result.seen} orders seen)')

    def _check_silence(self, results, silence_hours):
        """Exit 2 when SAP has reported nothing for too long.

        Deliberately NOT an error on its own. A company can genuinely have no
        planned orders for an hour. What is never normal is a whole day of
        nothing, and that is the shape the previous failure took.
        """
        quiet = [r.company for r in results if r.quiet]
        if not quiet:
            return

        self.stdout.write(self.style.WARNING(
            f'  nothing planned in: {", ".join(quiet)}'))

        cutoff = timezone.now() - timezone.timedelta(hours=silence_hours)
        stale = []
        for company in quiet:
            last = sync_service.last_sync_at(company)
            if last is None or last < cutoff:
                stale.append((company, last))

        if not stale:
            return

        for company, last in stale:
            seen = last.isoformat() if last else 'never'
            self.stderr.write(self.style.ERROR(
                f'  {company}: SAP has reported no planned order since {seen} '
                f'(threshold {silence_hours}h). Either production really has '
                f'stopped, or this feed has. Check before assuming the first.'))
        sys.exit(2)
