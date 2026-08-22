"""Backfill the v2 scheme tables from the legacy flat model.

Reads `scheme_product` (users.SchemeProduct) and `party_product_assignments`
(users.PartyProductAssignment) and produces Scheme / SchemeBenefit /
SchemeTrigger / SchemeAssignment rows, then snapshots `benefit_item_code` onto
existing `order_item_schemes` rows so history freezes at its present meaning.

The backfill is **behaviour-preserving**: every migrated benefit gets
per_qty = free_qty = 0, i.e. "quantity supplied by the user", which is exactly
what the legacy path does. Ratios are filled in afterwards, per scheme, by hand.

Idempotent — re-running updates in place rather than duplicating.

    python manage.py migrate_schemes_v2 --dry-run
    python manage.py migrate_schemes_v2

See docs/scheme-architecture.md.
"""

import re
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction

from orders.models import (
    OrderItemScheme,
    Scheme,
    SchemeAssignment,
    SchemeBenefit,
    SchemeTrigger,
)
from users.models import PartyProductAssignment, SchemeProduct


def slugify_code(name, taken):
    """Turn a scheme_name into a stable unique `code`.

    scheme_name is free text ("1 free pcs on 10 boxes"), so the slug is truncated
    and de-duplicated with a numeric suffix rather than trusted to be unique.
    """
    base = re.sub(r'[^A-Z0-9]+', '-', str(name or '').upper()).strip('-')[:40] or 'SCHEME'
    code = base
    suffix = 2
    while code in taken:
        code = f'{base}-{suffix}'
        suffix += 1
    taken.add(code)
    return code


class Command(BaseCommand):
    help = 'Backfill v2 scheme tables (schemes/benefits/triggers/assignments) from scheme_product.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report what would be written and roll back.')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        stats = defaultdict(int)

        with transaction.atomic():
            legacy_to_scheme = self._migrate_offers(stats)
            self._migrate_party_assignments(legacy_to_scheme, stats)
            self._snapshot_order_history(stats)

            if dry_run:
                self.stdout.write(self.style.WARNING('\n--dry-run: rolling back.'))
                transaction.set_rollback(True)

        self.stdout.write('')
        for key in sorted(stats):
            self.stdout.write(f'  {key:.<40} {stats[key]}')
        if not dry_run:
            self.stdout.write(self.style.SUCCESS('\nBackfill complete.'))
            self.stdout.write(
                'Next: fill in per_qty/free_qty on scheme_benefits for any scheme that '
                'should compute its own quantity. Until then every migrated scheme '
                'behaves exactly as it does today.'
            )

    # -- step 1: scheme_product -> Scheme + SchemeBenefit + STATE assignments --

    def _migrate_offers(self, stats):
        """Group scheme_product rows by scheme_name — that grouping *is* the
        legacy definition of a multi-item scheme (sync_service joins on the name),
        so it is what the child tables have to reproduce."""
        groups = defaultdict(list)
        for row in SchemeProduct.objects.all().order_by('scheme_id'):
            groups[(row.scheme_name or '').strip()].append(row)

        taken_codes = set(Scheme.objects.values_list('code', flat=True))
        # legacy scheme_id -> new Scheme, used by the history snapshot below.
        legacy_to_scheme = {}

        for scheme_name, rows in groups.items():
            if not scheme_name:
                stats['skipped_unnamed_scheme_product_rows'] += len(rows)
                continue

            scheme = Scheme.objects.filter(name=scheme_name).first()
            if scheme is None:
                scheme = Scheme.objects.create(
                    code=slugify_code(scheme_name, taken_codes),
                    name=scheme_name,
                    description='Migrated from scheme_product.',
                    # A group is live if any of its legacy rows was.
                    is_active=any(r.is_active for r in rows),
                )
                stats['schemes_created'] += 1
            else:
                taken_codes.add(scheme.code)
                stats['schemes_reused'] += 1

            for row in rows:
                legacy_to_scheme[row.scheme_id] = scheme

            # One benefit per distinct giveaway item — replaces the name-based
            # fan-out in sync_service.get_scheme_item_codes_for_combo.
            for item_code in sorted({(r.item_code or '').strip() for r in rows if (r.item_code or '').strip()}):
                _, created = SchemeBenefit.objects.get_or_create(
                    scheme=scheme,
                    free_item_code=item_code,
                    defaults={'free_uom': 'PCS', 'per_qty': 0, 'free_qty': 0},
                )
                stats['benefits_created' if created else 'benefits_existing'] += 1

            # state_code was only a picker filter before; here it becomes a real
            # scope, so one row now reaches every vendor in that state.
            for state_code in sorted({(r.state_code or '').strip() for r in rows if (r.state_code or '').strip()}):
                _, created = SchemeAssignment.objects.get_or_create(
                    scheme=scheme,
                    scope_type=SchemeAssignment.SCOPE_STATE,
                    scope_value=state_code,
                    category='',
                    defaults={'is_active': True},
                )
                stats['state_assignments_created' if created else 'state_assignments_existing'] += 1

        return legacy_to_scheme

    # -- step 2: party_product_assignments -> PARTY assignments + ITEM triggers --

    def _migrate_party_assignments(self, legacy_to_scheme, stats):
        assignments = (
            PartyProductAssignment.objects
            .filter(scheme__isnull=False, is_active=True)
            .order_by('id')
        )

        for row in assignments:
            scheme = legacy_to_scheme.get(row.scheme_id)
            if scheme is None:
                stats['party_rows_with_unknown_scheme'] += 1
                continue

            item_code = (row.item_code or '').strip()
            if item_code:
                # The legacy model had no explicit trigger — the assignment's
                # item_code was it. Make that implicit link explicit.
                _, created = SchemeTrigger.objects.get_or_create(
                    scheme=scheme,
                    match_type=SchemeTrigger.MATCH_ITEM,
                    match_value=item_code,
                    defaults={'min_qty': 0, 'min_uom': 'QTY', 'applies_to': 'PAID_LINE'},
                )
                stats['triggers_created' if created else 'triggers_existing'] += 1

            _, created = SchemeAssignment.objects.get_or_create(
                scheme=scheme,
                scope_type=SchemeAssignment.SCOPE_PARTY,
                scope_value=row.card_code,
                category=row.category or '',
                defaults={'is_active': True},
            )
            stats['party_assignments_created' if created else 'party_assignments_existing'] += 1

        # A scheme with no trigger never fires. `party_product_assignments.scheme_id`
        # is the only place the legacy model recorded which product earns a scheme,
        # and on deployments where the Add Sales picker sets the scheme straight on
        # the order line that column is empty — so there is genuinely nothing to
        # migrate. Those schemes need a trigger typed in before they work in v2.
        orphans = (
            Scheme.objects
            .filter(triggers__isnull=True, is_active=True)
            .values_list('code', flat=True)
        )
        for code in orphans:
            stats['schemes_without_triggers'] += 1
            self.stdout.write(self.style.WARNING(
                f'  Scheme {code}: no trigger. The legacy data never recorded which '
                'product earns it, so set one on the Schemes page before activating.'
            ))

    # -- step 3: freeze existing order history --

    def _snapshot_order_history(self, stats):
        """Copy each historical line's giveaway item onto the row itself.

        Today `sync_service` re-resolves the item from `scheme_product` at push
        time, so editing a scheme changes what an already-approved order ships.
        Snapshotting removes that.
        """
        legacy_items = dict(
            SchemeProduct.objects.values_list('scheme_id', 'item_code')
        )

        pending = []
        rows = OrderItemScheme.objects.filter(benefit_item_code__isnull=True).only(
            'id', 'scheme_id', 'qty_scheme'
        )
        for row in rows.iterator():
            item_code = (legacy_items.get(row.scheme_id) or '').strip()
            if not item_code:
                stats['history_rows_unresolvable'] += 1
                continue
            row.benefit_item_code = item_code
            row.computed_qty = row.qty_scheme or 0
            pending.append(row)
            if len(pending) >= 1000:
                OrderItemScheme.objects.bulk_update(
                    pending, ['benefit_item_code', 'computed_qty'])
                stats['history_rows_snapshotted'] += len(pending)
                pending = []

        if pending:
            OrderItemScheme.objects.bulk_update(pending, ['benefit_item_code', 'computed_qty'])
            stats['history_rows_snapshotted'] += len(pending)
