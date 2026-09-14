"""Email each stage's users about invoices stuck there beyond the threshold.

For every in-progress invoice whose dwell time at its current stage exceeds
that stage's `threshold_days`, the users mapped to that stage are emailed a
digest listing the invoice number and vendor (party) name.

Rules:
  * Pre-audit FULL holds are skipped — a full hold parks the invoice on
    purpose, so it should not raise a "stuck" email.
  * Invoices a desk user has MUTED (tracker.models.AlertMute) are skipped, with
    the reason they gave. The mute is tied to the stage visit, so it lapses on
    its own the moment the invoice moves to the next desk.
  * A re-notify cooldown (settings.TRACKER_ALERT_EMAIL_COOLDOWN_HOURS, default
    24h) stops the periodic sweep from emailing the same stuck invoice every run.

Idempotent — safe to schedule every N minutes (mirrors scan_stuck_alerts /
the auto-IRN sweep). Uses the StuckAlert row as the notify ledger.

    python manage.py email_stuck_alerts [--dry-run] [--force] [--cooldown-hours N]
"""
from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.utils import timezone

from tracker import services
from tracker.models import AlertNotification, StuckAlert

PRE_AUDIT = 'pre_audit'


class Command(BaseCommand):
    help = "Email stage users about invoices stuck beyond their stage threshold."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help="Show who would be emailed; send nothing.")
        parser.add_argument('--force', action='store_true',
                            help="Ignore the re-notify cooldown.")
        parser.add_argument('--cooldown-hours', type=int, default=None,
                            help="Override the re-notify cooldown (hours).")

    def handle(self, *args, **opts):
        now = timezone.now()
        dry_run = opts['dry_run']
        force = opts['force']
        cooldown_hours = (opts['cooldown_hours']
                          if opts['cooldown_hours'] is not None
                          else getattr(settings, 'TRACKER_ALERT_EMAIL_COOLDOWN_HOURS', 24))
        cooldown = timedelta(hours=cooldown_hours)

        # 1. Collect stuck visits due for notification.
        due = []           # dicts: alert, invoice, stage, days
        skipped_hold = skipped_muted = 0
        stuck = services.stuck_visits(now)
        # Resolved in one query for the whole sweep rather than per invoice.
        muted = services.alert_mute_map([inv.id for inv, _s, _d in stuck])
        for inv, stage, days in stuck:
            if stage.code == PRE_AUDIT and services.is_full_hold(inv):
                skipped_hold += 1
                continue
            if inv.id in muted:
                # Still stuck, still alerted on-screen — just not chased by mail.
                skipped_muted += 1
                continue

            alert, _created = StuckAlert.objects.get_or_create(
                invoice=inv, stage=stage,
                stage_entered_at=inv.current_stage_entered_at,
                defaults={'days_stuck': days,
                          'threshold_days': stage.threshold_days,
                          'is_active': True},
            )
            # Keep the alert fresh (also reactivate if it had been resolved).
            alert.days_stuck = days
            alert.threshold_days = stage.threshold_days
            if not alert.is_active:
                alert.is_active = True
                alert.resolved_at = None
            alert.save(update_fields=['days_stuck', 'threshold_days',
                                      'is_active', 'resolved_at', 'updated_at'])

            if (not force and alert.last_notified_at
                    and (now - alert.last_notified_at) < cooldown):
                continue  # still within cooldown
            due.append({'alert': alert, 'invoice': inv, 'stage': stage, 'days': days})

        if not due:
            self.stdout.write(self.style.SUCCESS(
                f"No stuck invoices due for notification "
                f"(skipped {skipped_hold} pre-audit full-hold, "
                f"{skipped_muted} muted)."))
            return

        # 2. Group by stage, then fan out to that stage's users.
        by_stage = {}
        for d in due:
            by_stage.setdefault(d['stage'].id, {'stage': d['stage'], 'items': []})
            by_stage[d['stage'].id]['items'].append(d)

        user_items = {}                 # user -> list of due dicts
        no_recipient_stages = []
        for entry in by_stage.values():
            stage = entry['stage']
            recipients = services.stage_recipients(stage)
            if not recipients:
                no_recipient_stages.append(stage.name)
                continue
            for user in recipients:
                user_items.setdefault(user.id, {'user': user, 'items': []})
                user_items[user.id]['items'].extend(entry['items'])

        # 3. Send one digest per user; stamp the alerts that actually went out.
        sent, notified_alert_ids = 0, set()
        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', None)
        for entry in user_items.values():
            user, items = entry['user'], entry['items']
            subject, body = self._compose(user, items)
            if dry_run:
                self.stdout.write(
                    f"[dry-run] would email {user.email}: {len(items)} invoice(s)")
                continue
            try:
                send_mail(subject, body, from_email, [user.email], fail_silently=False)
                sent += 1
                notified_alert_ids.update(i['alert'].id for i in items)
                # Audit trail: who was mailed about which invoice, and when.
                AlertNotification.objects.bulk_create([
                    AlertNotification(
                        alert=i['alert'], invoice=i['invoice'], stage=i['stage'],
                        user=user, email=user.email or '',
                        days_stuck=i['days'], sent_at=now,
                    ) for i in items
                ])
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(self.style.ERROR(
                    f"Failed to email {user.email}: {type(exc).__name__}: {exc}"))

        if notified_alert_ids and not dry_run:
            StuckAlert.objects.filter(id__in=notified_alert_ids).update(
                last_notified_at=now)

        msg = (f"Stuck-alert email: {sent} sent, {len(due)} due visit(s), "
               f"{skipped_hold} pre-audit full-hold skipped, "
               f"{skipped_muted} muted skipped.")
        if no_recipient_stages:
            msg += (f" No stage users mapped for: "
                    f"{', '.join(sorted(set(no_recipient_stages)))}.")
        self.stdout.write(self.style.SUCCESS(msg))

    @staticmethod
    def _compose(user, items):
        """Build (subject, body) for one user's digest, grouped by stage."""
        by_stage = {}
        for it in items:
            by_stage.setdefault(it['stage'].id, {'stage': it['stage'], 'rows': []})
            by_stage[it['stage'].id]['rows'].append(it)

        total = len(items)
        subject = f"[OMS Tracker] {total} invoice(s) pending at your desk beyond the allowed days"

        greeting = getattr(user, 'name', '') or user.username
        lines = [f"Hello {greeting},", "",
                 "The following invoices have been pending at your stage(s) "
                 "beyond the allowed number of days and need your action:", ""]
        for grp in by_stage.values():
            stage = grp['stage']
            lines.append(f"Stage: {stage.name}  (allowed {stage.threshold_days} day(s))")
            for it in sorted(grp['rows'], key=lambda r: float(r['days']), reverse=True):
                inv = it['invoice']
                lines.append(
                    f"   - Invoice {inv.invoice_number}  |  Vendor: {inv.party_name}"
                    f"  |  {it['days']} day(s) at this stage")
            lines.append("")
        lines += ["Please action these at the earliest.", "",
                  "- OMS Document Tracker (automated alert)"]
        return subject, "\n".join(lines)
