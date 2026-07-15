"""Scan for invoices stuck beyond their stage threshold and raise/resolve alerts.

Idempotent — safe to run every N minutes from Task Scheduler (mirrors the
auto-IRN sweep pattern). Creates a StuckAlert for each in-progress invoice
whose dwell time at its current stage exceeds that stage's threshold_days, and
resolves alerts whose invoice has since moved on or completed.

    python manage.py scan_stuck_alerts
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from tracker.models import Invoice, StuckAlert
from tracker.services import days_at_stage


class Command(BaseCommand):
    help = 'Raise/resolve stuck-invoice alerts based on per-stage thresholds.'

    @transaction.atomic
    def handle(self, *args, **options):
        now = timezone.now()
        open_invoices = (
            Invoice.objects
            .filter(status=Invoice.Status.IN_PROGRESS)
            .select_related('current_stage')
        )

        raised, updated = 0, 0
        seen_keys = set()

        for inv in open_invoices:
            stage = inv.current_stage
            days = days_at_stage(inv, now)
            if days <= stage.threshold_days:
                continue

            key = (inv.id, stage.id, inv.current_stage_entered_at)
            seen_keys.add(key)
            alert, created = StuckAlert.objects.get_or_create(
                invoice=inv, stage=stage,
                stage_entered_at=inv.current_stage_entered_at,
                defaults={
                    'days_stuck': days,
                    'threshold_days': stage.threshold_days,
                    'is_active': True,
                },
            )
            if created:
                raised += 1
            else:
                alert.days_stuck = days
                alert.threshold_days = stage.threshold_days
                if not alert.is_active:
                    alert.is_active = True
                    alert.resolved_at = None
                alert.save(update_fields=['days_stuck', 'threshold_days',
                                          'is_active', 'resolved_at', 'updated_at'])
                updated += 1

        # Resolve any active alert that no longer corresponds to a current,
        # still-stuck visit (invoice moved, completed, or threshold raised).
        resolved = 0
        for alert in StuckAlert.objects.filter(is_active=True).select_related(
            'invoice', 'invoice__current_stage'
        ):
            key = (alert.invoice_id, alert.stage_id, alert.stage_entered_at)
            if key not in seen_keys:
                alert.is_active = False
                alert.resolved_at = now
                alert.save(update_fields=['is_active', 'resolved_at', 'updated_at'])
                resolved += 1

        self.stdout.write(self.style.SUCCESS(
            f'Stuck-alert scan: {raised} raised, {updated} updated, {resolved} resolved. '
            f'Active now: {StuckAlert.objects.filter(is_active=True).count()}.'
        ))
