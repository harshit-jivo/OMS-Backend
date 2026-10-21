"""India's clock, for what SAP and the people using OMS call "today".

This server runs on UTC (`settings.TIME_ZONE`), 5½ hours behind India, so
`timezone.localdate()` is still yesterday until 05:30 IST: a payment posted
after midnight would carry the previous day's date in SAP, an expense would
default to the wrong day or month, and a request raised in the first hours of
1 January would be numbered in the old year. Everything here that means
"today" asks this module instead.
"""
from datetime import date, time
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

#: SAP's clock, and the business's.
INDIA = ZoneInfo('Asia/Kolkata')


def now():
    """The current moment, on India's clock."""
    return timezone.now().astimezone(INDIA)


def today():
    """Today's date in India."""
    return now().date()


def _time(value, default):
    try:
        hours, minutes = str(value or default).split(':')
        return time(int(hours), int(minutes))
    except (TypeError, ValueError):
        return time(*map(int, default.split(':')))


def cutoffs():
    """`(everyone, audit)`: when the day's Payments close on India's clock (user,
    2026-10-21) — 4:30 pm for everyone but Audit, 6:30 pm for Audit too.
    `ADVANCE_PAYMENT_CUTOFF` / `ADVANCE_PAYMENT_AUDIT_CUTOFF` ("HH:MM") move them."""
    return (_time(getattr(settings, 'ADVANCE_PAYMENT_CUTOFF', ''), '16:30'),
            _time(getattr(settings, 'ADVANCE_PAYMENT_AUDIT_CUTOFF', ''), '18:30'))


def cutoff_from():
    """The first day the cut-off applies (user, 2026-10-21: "from tomorrow"):
    `ADVANCE_PAYMENT_CUTOFF_FROM` ("YYYY-MM-DD"), 22 Oct 2026 by default."""
    value = getattr(settings, 'ADVANCE_PAYMENT_CUTOFF_FROM', '') or '2026-10-22'
    try:
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    except ValueError:
        return date(2026, 10, 22)


def closed_for(at_audit, moment=None):
    """Why nothing may be done now, or '' when it may: after the day's cut-off,
    until midnight — from `cutoff_from()` on. `at_audit`: the request waits at
    an Audit stage, which may act until the later cut-off."""
    everyone, audit = cutoffs()
    moment = (moment or now()).astimezone(INDIA)
    if moment.date() < cutoff_from():
        return ''
    current = moment.time()
    if current >= audit:
        return (f'Payments are closed for today after {audit:%I:%M %p} (IST), Audit included. '
                f'Try again tomorrow.')
    if current >= everyone and not at_audit:
        return (f'Payments are closed for today after {everyone:%I:%M %p} (IST): only Audit may act until '
                f'{audit:%I:%M %p}. Try again tomorrow.')
    return ''
