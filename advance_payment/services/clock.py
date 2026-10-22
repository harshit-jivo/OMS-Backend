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
    """`(raising, desks)`: when the day's Payments close on India's clock (user,
    2026-10-22) — 4:30 pm for raising a new request, 6:30 pm for every desk too.
    `ADVANCE_PAYMENT_CUTOFF` / `ADVANCE_PAYMENT_AUDIT_CUTOFF` ("HH:MM") move them."""
    return (_time(getattr(settings, 'ADVANCE_PAYMENT_CUTOFF', ''), '16:30'),
            _time(getattr(settings, 'ADVANCE_PAYMENT_AUDIT_CUTOFF', ''), '18:30'))


#: The first day the cut-off applies (user, 2026-10-22: "apply lock from monday
#: onwards, for now remove the locks").
FIRST_DAY = date(2026, 10, 26)


def cutoff_from():
    """The first day the cut-off applies: `ADVANCE_PAYMENT_CUTOFF_FROM`
    ("YYYY-MM-DD"), Monday 26 Oct 2026 by default."""
    value = getattr(settings, 'ADVANCE_PAYMENT_CUTOFF_FROM', '') or FIRST_DAY
    try:
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    except ValueError:
        return FIRST_DAY


def closed_for(raising, moment=None):
    """Why it may not be done now, or '' when it may — from `cutoff_from()` on,
    until midnight (user, 2026-10-22): after 4:30 pm no new request is raised
    (`raising`); the desks work on until 6:30 pm, and after that nothing moves."""
    first, last = cutoffs()
    moment = (moment or now()).astimezone(INDIA)
    if moment.date() < cutoff_from():
        return ''
    current = moment.time()
    if current >= last:
        return f'Payments are closed for today after {last:%I:%M %p} (IST). Try again tomorrow.'
    if current >= first and raising:
        return (f'New payment requests are closed for today after {first:%I:%M %p} (IST); the desks work on '
                f'until {last:%I:%M %p}. Raise it tomorrow.')
    return ''
