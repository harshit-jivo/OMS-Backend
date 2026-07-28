"""Derived device activity status.

Status is NOT stored — it is a pure function of `last_active` and "now", so it
can never go stale and needs no schema change. This module is the single source
of truth for the thresholds and is shared by:

  * the admin serializer  -> the per-row `status` field (what the badge shows)
  * the admin list filter -> ?status=online|idle|offline|inactive
  * the analytics view    -> the summary-card counts

Keeping all three on one definition means a filtered list, its badges and the
cards can never disagree. It is also why the SERVER computes the status rather
than the browser: a client with a skewed clock would otherwise render a badge
that contradicts the server-side filter it just asked for.

Buckets are mutually exclusive and exhaustive, so the four counts always sum to
the total:

    online    last_active >= now - 5m
    idle      now - 30m  <= last_active < now - 5m
    offline   now - 30d  <= last_active < now - 30m
    inactive  last_active < now - 30d

Note: the spec describes "offline" as older than 30 minutes and "inactive" as
older than 30 days, which overlap. They are treated as distinct ranges here —
"inactive" is the long tail of "offline" — so a device lands in exactly one
bucket and the cards add up.
"""
from datetime import timedelta

from django.db.models import Count, Q

ONLINE_WITHIN_MINUTES = 5
IDLE_WITHIN_MINUTES = 30
INACTIVE_AFTER_DAYS = 30

STATUS_ONLINE = "online"
STATUS_IDLE = "idle"
STATUS_OFFLINE = "offline"
STATUS_INACTIVE = "inactive"

STATUS_VALUES = (STATUS_ONLINE, STATUS_IDLE, STATUS_OFFLINE, STATUS_INACTIVE)


def boundaries(now):
    """Return the three cut-off timestamps used by every rule below."""
    return {
        "online_from": now - timedelta(minutes=ONLINE_WITHIN_MINUTES),
        "idle_from": now - timedelta(minutes=IDLE_WITHIN_MINUTES),
        "inactive_before": now - timedelta(days=INACTIVE_AFTER_DAYS),
    }


def compute_status(last_active, now):
    """Derive the status of one device. Returns one of STATUS_VALUES."""
    if last_active is None:
        return STATUS_INACTIVE
    edge = boundaries(now)
    if last_active >= edge["online_from"]:
        return STATUS_ONLINE
    if last_active >= edge["idle_from"]:
        return STATUS_IDLE
    if last_active >= edge["inactive_before"]:
        return STATUS_OFFLINE
    return STATUS_INACTIVE


def filter_by_status(queryset, status, now):
    """Narrow `queryset` to one status bucket.

    An unknown/blank status returns the queryset untouched, so a bad query
    param degrades to "no filter" rather than an error.
    """
    if status not in STATUS_VALUES:
        return queryset
    edge = boundaries(now)
    if status == STATUS_ONLINE:
        return queryset.filter(last_active__gte=edge["online_from"])
    if status == STATUS_IDLE:
        return queryset.filter(
            last_active__gte=edge["idle_from"], last_active__lt=edge["online_from"]
        )
    if status == STATUS_OFFLINE:
        return queryset.filter(
            last_active__gte=edge["inactive_before"], last_active__lt=edge["idle_from"]
        )
    return queryset.filter(last_active__lt=edge["inactive_before"])


def status_counts(queryset, now):
    """Count all four buckets in a SINGLE query via conditional aggregation."""
    edge = boundaries(now)
    return queryset.aggregate(
        online=Count("id", filter=Q(last_active__gte=edge["online_from"])),
        idle=Count(
            "id",
            filter=Q(
                last_active__gte=edge["idle_from"],
                last_active__lt=edge["online_from"],
            ),
        ),
        offline=Count(
            "id",
            filter=Q(
                last_active__gte=edge["inactive_before"],
                last_active__lt=edge["idle_from"],
            ),
        ),
        inactive=Count("id", filter=Q(last_active__lt=edge["inactive_before"])),
    )


def thresholds():
    """The rule set, exposed to clients so the UI can explain itself."""
    return {
        "online_within_minutes": ONLINE_WITHIN_MINUTES,
        "idle_within_minutes": IDLE_WITHIN_MINUTES,
        "inactive_after_days": INACTIVE_AFTER_DAYS,
    }
