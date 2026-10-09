"""India's clock, for what SAP and the people using OMS call "today".

This server runs on UTC (`settings.TIME_ZONE`), 5½ hours behind India, so
`timezone.localdate()` is still yesterday until 05:30 IST: a payment posted
after midnight would carry the previous day's date in SAP, an expense would
default to the wrong day or month, and a request raised in the first hours of
1 January would be numbered in the old year. Everything here that means
"today" asks this module instead.
"""
from zoneinfo import ZoneInfo

from django.utils import timezone

#: SAP's clock, and the business's.
INDIA = ZoneInfo('Asia/Kolkata')


def now():
    """The current moment, on India's clock."""
    return timezone.now().astimezone(INDIA)


def today():
    """Today's date in India."""
    return now().date()
