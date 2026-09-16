"""Pulling production orders out of SAP, and retiring the ones SAP moved on.

THIS IS THE PART THAT BROKE LAST TIME
--------------------------------------
JSAP's feed into `PRDO.PlannedProductionOrders` stopped on 13 Aug 2026 and its
approval job kept logging "The job succeeded" every 60 seconds for 33 days,
because its intake was `WHERE Status = 'P'` and an empty source is not an
error. 432 production orders were raised in that window and none entered any
approval. See `docs/backend/PRODUCTION_ORDER_JSAP_SAP.md` §5.

So the rules here are about being noisy, not about being clever:

* an unreachable SAP RAISES (`sap.SapUnavailable`) — it never returns `[]`
* a silent run is reported: `SyncResult.quiet` is true when nothing at all was
  seen, and the management command turns that into a non-zero exit once the
  silence outlasts `PRODUCTION_SYNC_SILENCE_HOURS`
* every run stamps `synced_at`, so "when did this last work?" is answerable
  from the data rather than from Task Scheduler

TWO PASSES
----------
    intake     SAP Planned orders   -> upsert + open a flow for new ones
    reconcile  OMS pending flows    -> retire the ones SAP no longer has Planned

Volume is small — 23 Standard non-RM orders are Planned in OIL at the time of
writing, and the feed averaged 3-29 rows a day. No watermark, no change
tracking: a full scan of `Status = 'P'` is cheaper than the bookkeeping would
be, and it cannot drift.
"""
import logging
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

from production.models import (
    FlowStatus,
    LogAction,
    ProductionOrder,
    ProductionOrderFlow,
    SapOrderStatus,
)
from production.services import flow as flow_service
from production.services import sap as sap_service

logger = logging.getLogger(__name__)

#: Snapshot columns the sync refreshes while a request is still pending, and
#: whose changes are worth recording. Deliberately not every column: a
#: `synced_at` diff on every run would drown the log it is meant to explain.
TRACKED_FIELDS = (
    'item_code', 'item_name', 'item_group', 'item_series', 'warehouse',
    'planned_qty', 'sal_factor2', 'sal_pack_un', 'order_type',
    'post_date', 'due_date', 'start_date',
    'batch_no', 'mfg_date', 'expiry_date',
    'sap_created_by', 'sap_user_sign', 'remarks', 'sap_doc_num',
)


@dataclass
class SyncResult:
    company: str
    seen: int = 0
    created: int = 0
    updated: int = 0
    retired: int = 0
    unroutable: list = field(default_factory=list)
    #: Dry run only: where each new order WOULD be routed. Populated by
    #: running selection for real and rolling it back, so it reflects the
    #: configuration as it actually stands rather than as it is meant to.
    routed: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def quiet(self):
        """SAP reported nothing at all. Not an error — but not nothing, either.

        The command decides what to do about it, because whether silence is
        suspicious depends on how long it has lasted.
        """
        return self.seen == 0 and not self.errors

    def as_dict(self):
        return {
            'company': self.company, 'seen': self.seen,
            'created': self.created, 'updated': self.updated,
            'retired': self.retired, 'unroutable': self.unroutable,
            'routed': self.routed, 'errors': self.errors,
        }


def _diff(order, incoming):
    """Changed tracked fields, as {field: {old, new}} — or None."""
    changes = {}
    for name in TRACKED_FIELDS:
        if name not in incoming:
            continue
        old, new = getattr(order, name), incoming[name]
        # Decimals and dates compare correctly; normalise to str for the log
        # so the JSON is stable and readable.
        if old != new:
            changes[name] = {
                'old': None if old is None else str(old),
                'new': None if new is None else str(new),
            }
    return changes or None


@transaction.atomic
def _upsert(company, row, result, *, dry_run=False):
    """Create or refresh one order, opening a flow the first time we see it."""
    order = (ProductionOrder.objects
             .select_for_update()
             .filter(company=company, sap_doc_entry=row['sap_doc_entry'])
             .first())
    now = timezone.now()

    if order is None:
        # A dry run does the REAL work and then rolls it back, rather than
        # counting the row and returning early.
        #
        # That matters because workflow selection only happens when the row
        # exists: the engine evaluates each configured condition against the
        # actual relation, keyed on the row's id. Skipping the insert would
        # make `--dry-run` report "N would be created" while staying silent
        # about the orders that cannot be ROUTED — which is the failure worth
        # knowing about before the first real run, and the one JSAP shipped.
        order = ProductionOrder(company=company, synced_at=now, **row)
        order.save()
        try:
            flow_service.open_flow(order)
        except flow_service.ProductionFlowError as exc:
            # No workflow matched, or it has no stages. The order is rolled
            # back with the flow: an order OMS cannot route must not sit in
            # the list looking like it is waiting for somebody. JSAP left
            # exactly those rows behind, silently.
            result.unroutable.append({
                'doc_entry': row['sap_doc_entry'],
                'item_code': row['item_code'],
                'reason': str(exc),
            })
            transaction.set_rollback(True)
            return
        result.created += 1
        if dry_run:
            # Same savepoint rollback the unroutable path uses, so nothing
            # persists. The id sequence advances; that is harmless and is the
            # only trace a dry run leaves.
            result.routed.append({
                'doc_entry': row['sap_doc_entry'],
                'item_code': row['item_code'],
                'workflow': order.flow.workflow.code,
                'stage': order.flow.current_stage.name if order.flow.current_stage else None,
                'user': getattr(order.flow.current_user, 'username', None),
            })
            transaction.set_rollback(True)
        return

    # Already known. Refresh the snapshot only while the decision is still
    # open — once approved or rejected, the record must keep saying what was
    # actually decided on.
    open_flow = getattr(order, 'flow', None)
    if open_flow is not None and not open_flow.is_open:
        return

    changes = _diff(order, row)
    if not changes and order.sap_status == row['sap_status']:
        return
    if dry_run:
        result.updated += 1
        return

    for name, value in row.items():
        setattr(order, name, value)
    order.synced_at = now
    order.save()
    flow_service.log(order, action=LogAction.SYNC,
                     remarks='Refreshed from SAP.', action_data=changes)
    result.updated += 1


def intake(company, *, dry_run=False):
    """Pull SAP's Planned orders into OMS. Raises `SapUnavailable` on failure."""
    result = SyncResult(company=company)
    rows = sap_service.planned_orders(company)
    result.seen = len(rows)

    for row in rows:
        try:
            _upsert(company, row, result, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 — one bad row must not stop the run
            logger.exception('PRDO: upsert failed for %s DocEntry %s',
                             company, row.get('sap_doc_entry'))
            result.errors.append({
                'doc_entry': row.get('sap_doc_entry'),
                'error': f'{type(exc).__name__}: {exc}',
            })
    return result


def reconcile(company, result, *, dry_run=False):
    """Retire pending requests whose SAP order is no longer Planned.

    The exit JSAP never had. Without it a request whose order was released,
    closed or cancelled elsewhere waits forever — 17 of JSAP's have been
    waiting since as far back as October 2025.
    """
    pending = list(
        ProductionOrderFlow.objects
        .filter(status=FlowStatus.PENDING,
                production_order__company=company)
        .select_related('production_order')
    )
    if not pending:
        return result

    doc_entries = [f.production_order.sap_doc_entry for f in pending]
    statuses = sap_service.statuses_for(company, doc_entries)

    for flow in pending:
        order = flow.production_order
        current = statuses.get(order.sap_doc_entry)
        if current == SapOrderStatus.PLANNED:
            continue

        if current is None:
            reason = ('The production order no longer exists in SAP.')
        else:
            label = dict(SapOrderStatus.choices).get(current, current)
            reason = (f'SAP moved this order to {label} before it was '
                      f'decided, so the approval is no longer needed.')

        if dry_run:
            result.retired += 1
            continue

        if current is not None and order.sap_status != current:
            order.sap_status = current
            order.synced_at = timezone.now()
            order.save(update_fields=['sap_status', 'synced_at', 'updated_at'])
        flow_service.retire(flow, reason=reason)
        result.retired += 1
    return result


def run(company, *, dry_run=False):
    """One full sweep for one company: intake, then reconcile."""
    result = intake(company, dry_run=dry_run)
    reconcile(company, result, dry_run=dry_run)
    return result


def last_sync_at(company=None):
    """When the newest snapshot for `company` was refreshed, or None.

    Backs `/api/production/health/` and the command's silence check. Reading
    it from the data rather than from a dedicated marker means it cannot claim
    a run that wrote nothing.
    """
    qs = ProductionOrder.objects.all()
    if company:
        qs = qs.filter(company=company)
    return qs.order_by('-synced_at').values_list('synced_at', flat=True).first()
