"""Health and readiness — Phase 5.3.

Three endpoints, because "is it healthy" is three different questions asked by
three different callers:

    GET /api/health/live/    is the process alive?          (public, no I/O)
    GET /api/health/ready/   can it serve traffic?          (public, terse)
    GET /api/health/detail/  what exactly is wrong?         (admin only)

Two design decisions are worth stating, because both could reasonably have
gone the other way.

**Critical vs degraded.** OMS reads three HANA company databases and the SAP
Service Layer, and it is tempting to call all of them "required". They are
not. When HANA is unreachable the tracker, approvals, payments and the whole
order history still work — they live in Postgres. If `ready` returned 503 for
a HANA outage, a load balancer would pull every node out of rotation and turn
a partial outage into a total one. So Postgres is CRITICAL and everything
external is DEGRADED: reported, alerted on, but not a reason to stop serving.

**What `ready` tells an anonymous caller.** Nothing but component names and
up/down. Exception text from a failed database connect carries hostnames,
usernames, ports and driver versions, and this endpoint has to answer an
unauthenticated load balancer. The diagnosis lives at `/detail/`, behind
admin. `ready` is deliberately the least informative thing that is still
useful.

Nothing here is cached. A cached health check reports the state of the world
as it was, which is precisely the thing a health check must not do.
"""
import logging
import time

from django.conf import settings
from django.db import connections
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework import status as http_status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core.permissions import IsAdminRole

logger = logging.getLogger(__name__)

UP = 'up'
DOWN = 'down'
SKIPPED = 'skipped'

class Skip(str):
    """A probe's way of saying "there is nothing to check here, and that is
    correct" — carrying the REASON.

    A bare `None` was the first version and it reported every skip as "not
    configured", which was a lie for the scheduler probe: the scheduler is
    configured, there are simply no active schedules for it to run. A health
    check that misdescribes why it is quiet is worse than one that says
    nothing, because someone will act on the description.
    """


NOT_CONFIGURED = Skip('not configured')


#: A dependency whose loss stops the service answering usefully at all.
CRITICAL = 'critical'
#: A dependency whose loss removes some features and leaves the rest working.
DEGRADED = 'degraded'


class Check:
    """One dependency probe.

    `criticality` is the field that decides the HTTP status, and it is
    attached to the CHECK rather than computed at the call site so that adding
    a dependency forces the author to answer "does an outage here mean we stop
    serving?" — rather than defaulting to whichever answer the surrounding
    code happened to use.
    """

    def __init__(self, name, criticality, probe):
        self.name = name
        self.criticality = criticality
        self.probe = probe

    def run(self):
        started = time.monotonic()
        try:
            # A probe returns True for "healthy", a plain string for
            # "healthy, and here is what it said", or a `Skip` for "nothing to
            # check here, for this reason" — which is not a failure.
            detail = self.probe()
            state = SKIPPED if isinstance(detail, Skip) else UP
        except Exception as exc:
            # The message is kept OUT of the public payload — see the module
            # docstring — but it must reach the logs, or an outage is
            # observable only as a number.
            logger.warning('Health check %s failed', self.name, exc_info=exc)
            return {
                'name': self.name,
                'criticality': self.criticality,
                'status': DOWN,
                'latency_ms': round((time.monotonic() - started) * 1000, 1),
                'error': f'{type(exc).__name__}: {exc}',
            }
        result = {
            'name': self.name,
            'criticality': self.criticality,
            'status': state,
            'latency_ms': round((time.monotonic() - started) * 1000, 1),
        }
        if isinstance(detail, str):
            result['detail'] = detail
        return result


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

def probe_postgres():
    """The application database. `SELECT 1` on a connection Django hands out,
    so it exercises the same pool the rest of the app uses rather than opening
    a private one that could succeed while the real one is exhausted."""
    with connections['default'].cursor() as cursor:
        cursor.execute('SELECT 1')
        cursor.fetchone()
    return True


def probe_hana():
    """The three SAP company databases share one host and one credential, so
    one connect answers for all three. Returns None — reported as `skipped` —
    when HANA is not configured, which is the case in CI and in tests."""
    cfg = settings.DATABASES.get('hana') or {}
    if not cfg.get('HOST'):
        return NOT_CONFIGURED

    from hana.services.connection import HANAConnection

    conn = HANAConnection()
    try:
        conn.connect()
        conn.cursor.execute('SELECT 1 FROM DUMMY')
        conn.cursor.fetchone()
    finally:
        try:
            conn.disconnect()
        except Exception:
            # A failed disconnect must not turn a healthy probe into a red
            # one; the connection is being dropped either way.
            logger.debug('HANA health probe failed to disconnect', exc_info=True)
    return True


def probe_service_layer():
    """A TCP+TLS reach test against the Service Layer, NOT a login.

    Logging in would be a truer readiness signal and is the wrong thing to do
    on a public endpoint: SAP counts concurrent sessions and expires them on a
    timer, so a health check polled every few seconds would consume the licence
    and evict the sessions real orders are using. Reachability is the most this
    check may cost.
    """
    url = getattr(settings, 'HANA_SERVICE_LAYER_URL', '') or ''
    if not url:
        return NOT_CONFIGURED

    import requests

    from serviceLayer.service import _timeout, _verify

    response = requests.get(f'{url.rstrip("/")}/', timeout=_timeout(),
                            verify=_verify())
    # 401/403 means the Service Layer is up and refusing an anonymous caller,
    # which is exactly right. Only a transport failure or a 5xx is a problem.
    if response.status_code >= 500:
        raise RuntimeError(f'Service Layer returned {response.status_code}')
    return f'HTTP {response.status_code}'


def probe_scheduler():
    """Whether the 5.1 worker is actually running.

    Included because the failure this system already had was a scheduler that
    was silently absent for its entire life — see `sap_sync/scheduler.py`. The
    advisory lock makes that observable: if nobody holds it, nobody is
    scheduling. Reported as `skipped` when there are no active schedules,
    because then there is nothing to run and an idle lock is correct.
    """
    from sap_sync.models import SyncSchedule

    if not SyncSchedule.objects.filter(is_active=True).exists():
        return Skip('no active schedules')
    if connections['default'].vendor != 'postgresql':
        return Skip('advisory locks need Postgres')

    from sap_sync.management.commands.run_scheduler import ADVISORY_LOCK_KEY

    with connections['default'].cursor() as cursor:
        cursor.execute(
            'SELECT count(*) FROM pg_locks WHERE locktype = %s AND objid = %s',
            ['advisory', ADVISORY_LOCK_KEY])
        holders = cursor.fetchone()[0]
    if not holders:
        raise RuntimeError(
            'active schedules exist but no scheduler worker holds the lock')
    return f'{holders} worker(s)'


CHECKS = [
    Check('postgres', CRITICAL, probe_postgres),
    Check('hana', DEGRADED, probe_hana),
    Check('service_layer', DEGRADED, probe_service_layer),
    Check('scheduler', DEGRADED, probe_scheduler),
]


def run_checks(checks=None):
    """Run every probe and summarise.

    Every check runs even after one fails. A first-failure return would report
    the first problem repeatedly and hide the rest, which is the opposite of
    what someone reading this during an incident needs.
    """
    results = [check.run() for check in (checks if checks is not None else CHECKS)]
    critical_down = [
        r for r in results if r['status'] == DOWN and r['criticality'] == CRITICAL
    ]
    any_down = [r for r in results if r['status'] == DOWN]

    if critical_down:
        overall = DOWN
    elif any_down:
        overall = DEGRADED
    else:
        overall = UP
    return overall, results


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

#: Response shapes, declared so the endpoints appear in the OpenAPI document
#: with a real body rather than as an undescribed operation. These views return
#: plain dicts rather than model data, so there is no ModelSerializer to infer
#: from and `inline_serializer` is the whole definition.
_STATUS_FIELD = serializers.ChoiceField(choices=[UP, DOWN, DEGRADED])

LIVENESS_RESPONSE = inline_serializer(
    name='Liveness', fields={'status': serializers.ChoiceField(choices=[UP])})

READINESS_RESPONSE = inline_serializer(name='Readiness', fields={
    'status': _STATUS_FIELD,
    'checks': serializers.DictField(child=serializers.CharField()),
})

HEALTH_DETAIL_RESPONSE = inline_serializer(name='HealthDetail', fields={
    'status': _STATUS_FIELD,
    'checks': serializers.ListField(child=inline_serializer(
        name='HealthCheckResult', fields={
            'name': serializers.CharField(),
            'criticality': serializers.ChoiceField(choices=[CRITICAL, DEGRADED]),
            'status': serializers.ChoiceField(choices=[UP, DOWN, SKIPPED]),
            'latency_ms': serializers.FloatField(),
            'detail': serializers.CharField(required=False),
            'error': serializers.CharField(required=False),
        })),
})


@extend_schema(
    responses={200: LIVENESS_RESPONSE},
    description='Liveness probe. Touches no dependency and always answers 200 '
                'while the process is running.',
)
class LivenessView(APIView):
    """Is the process up? Nothing else.

    Touches no dependency on purpose. A liveness probe that checks the
    database will report the process as dead during a database outage, and an
    orchestrator will restart every node — which does not fix a database and
    does guarantee that nothing is left running when it comes back.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        return Response({'status': UP})


@extend_schema(
    responses={200: READINESS_RESPONSE, 503: READINESS_RESPONSE},
    description='Readiness probe. 503 only when a CRITICAL dependency is down; '
                'a degraded external dependency still answers 200.',
)
class ReadinessView(APIView):
    """Can this node serve traffic? Terse by design — see the module docstring.

    503 only when something CRITICAL is down, so that a HANA outage degrades
    the service instead of emptying the load balancer pool.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        overall, results = run_checks()
        return Response(
            {
                'status': overall,
                'checks': {r['name']: r['status'] for r in results},
            },
            status=(http_status.HTTP_503_SERVICE_UNAVAILABLE if overall == DOWN
                    else http_status.HTTP_200_OK),
        )


@extend_schema(
    responses={200: HEALTH_DETAIL_RESPONSE},
    description='Full health diagnosis, including error text. Admin only.',
)
class HealthDetailView(APIView):
    """The same probes with the diagnosis attached. Admin only, because the
    error strings name hosts, ports and drivers."""
    permission_classes = [IsAdminRole]

    def get(self, request):
        overall, results = run_checks()
        return Response({
            'status': overall,
            'checks': results,
        })
