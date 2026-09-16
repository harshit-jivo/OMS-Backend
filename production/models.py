"""PRDO — Production Order approval.

WHAT THIS MODULE IS
-------------------
A planner raises a production order in SAP B1. OMS notices it, routes it
through a configured approval chain, and writes the decision back so SAP will
allow the order to be Released.

**OMS never creates, edits, cancels or closes a production order.** Three
responsibilities only: notice it, decide it, tell SAP. That is why there is no
create endpoint and no `created_by` on the request — nobody in OMS raised it.

OWNERSHIP — WHY THESE THREE TABLES ARE HERE AND NOT IN `workflow`
------------------------------------------------------------------
The Workflow Engine answers exactly one question: *which workflow applies to
this document, and what stages/users are configured?* Everything after that is
this module's business, and lives in the `production` schema:

    ProductionOrder            the SAP document, snapshotted
    ProductionOrderFlow        where it currently is, and the SAP outcome
    ProductionOrderActionLog   what was decided, by whom, when

No task table — see `ProductionOrderFlow`. Same shape as BKDT
(`docs/backend/BKDT.md` §3), and for the same reasons.

WHAT JSAP GOT WRONG, AND WHERE IT IS FIXED HERE
------------------------------------------------
See `docs/backend/PRODUCTION_ORDER_JSAP_SAP.md` for the evidence.

* `jsDocEntry.docEntry` carried no company, and DocEntry sequences run PER
  COMPANY. 11 OIL orders ended up recorded against Beverages and one has been
  unactionable since April 2026. Here the key is
  `unique (company, sap_doc_entry)`, which makes that unrepresentable rather
  than merely fixed.
* `jsDocEntryDetail` kept a SECOND copy of itemCode/warehouse, written from a
  stale T-SQL loop variable — 18% of rows carry another order's item. Nothing
  is copied here: the request IS the record, written once from SAP.
* A pending order whose SAP status moved on had nowhere to go, so 17 sat
  Planned from as far back as Oct 2025. `FlowStatus.OBSOLETE` is that exit.
"""
from django.conf import settings
from django.db import models
from django.db.models import Q

from core.companies import COMPANY_CHOICES, COMPANY_CODES


def _t(table):
    """Schema-qualify a table for the dedicated `production` Postgres schema.

    The `schema"."table` form puts the table in its own schema without growing
    the global `search_path` — the same trick `HAIS`, `workflow` and
    `backdate` use.
    """
    return f'production"."{table}'


class SapOrderStatus(models.TextChoices):
    """`OWOR.Status`, as SAP spells it. Snapshotted, never decided here."""

    PLANNED = 'P', 'Planned'
    RELEASED = 'R', 'Released'
    CLOSED = 'L', 'Closed'
    CANCELLED = 'C', 'Cancelled'


class SapOrderType(models.TextChoices):
    """`OWOR.Type`.

    Only `S` is in scope today — JSAP never approved Special or Disassembly
    orders (702 of the 812 it missed). The column accepts all three so the
    decision is a workflow condition rather than a schema change.
    """

    STANDARD = 'S', 'Standard'
    SPECIAL = 'P', 'Special'
    DISASSEMBLY = 'D', 'Disassembly'


class FlowStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    APPROVED = 'APPROVED', 'Approved'
    REJECTED = 'REJECTED', 'Rejected'
    #: SAP moved the order out of Planned before anyone decided it. Not a
    #: decision and not a failure — the question simply stopped being asked.
    #: BKDT has no equivalent because a rights request cannot be withdrawn by
    #: SAP; a production order can.
    OBSOLETE = 'OBSOLETE', 'No longer planned in SAP'


class SapWriteStatus(models.TextChoices):
    SUCCESS = 'SUCCESS', 'Written to SAP'
    FAILED = 'FAILED', 'SAP write failed'


class LogAction(models.TextChoices):
    #: The sync created or refreshed the request. `acted_by` is NULL: no OMS
    #: user did this. Replaces BKDT's CREATE, which a person performed.
    SYNC = 'SYNC', 'Synced from SAP'
    APPROVE = 'APPROVE', 'Approved'
    REJECT = 'REJECT', 'Rejected'
    OBSOLETE = 'OBSOLETE', 'Retired — no longer planned in SAP'


class ProductionOrder(models.Model):
    """One SAP production order, snapshotted at the moment OMS first saw it.

    Every SAP-derived column is a SNAPSHOT, refreshed by the sync while the
    request is still pending and frozen thereafter. An item renamed or
    re-grouped in SAP six months later must not change what an approved order
    says it was — the same reasoning as `orders.OrderItemScheme.benefit_item_code`.

    There is no `created_by`. The SAP creator is recorded as
    `sap_created_by` / `sap_user_sign`, which are TEXT AND AN INTEGER, not a
    foreign key: a SAP login lives in a different identity namespace from an
    OMS user, and pointing an FK across that gap is how JSAP's `createdBy`
    came to mean two different things in two tables.
    """

    company = models.CharField(
        max_length=20, choices=COMPANY_CHOICES, db_index=True,
        help_text='The SAP company database this order belongs to.',
    )
    #: `OWOR.DocEntry` — the internal key, and what the SAP release gate joins
    #: on. Unique only WITH `company`; see the constraint below.
    sap_doc_entry = models.IntegerField()
    #: `OWOR.DocNum` — the number a human quotes. Not unique across companies
    #: either, and not used as a key.
    sap_doc_num = models.IntegerField(null=True, blank=True)

    item_code = models.CharField(max_length=50, db_index=True)
    item_name = models.CharField(max_length=200, blank=True, default='')
    #: `OITM.U_Sub_Group` and `OITM.Series`. Snapshotted so a workflow
    #: condition can move off the `PM%` item-code prefix — a naming convention
    #: — and onto SAP's own tagging, with no schema change. See
    #: `docs/backend/PRDO_DESIGN.md` §4.
    item_group = models.CharField(max_length=50, blank=True, default='')
    item_series = models.IntegerField(null=True, blank=True)

    warehouse = models.CharField(max_length=20, blank=True, default='')

    #: `OWOR.PlannedQty`, which is PIECES. Boxes and litres are derived from
    #: the two pack columns below rather than stored, because a derived number
    #: that disagrees with its inputs is worse than no number.
    planned_qty = models.DecimalField(max_digits=19, decimal_places=6)
    sal_factor2 = models.DecimalField(
        max_digits=19, decimal_places=6, null=True, blank=True,
        help_text='OITM.SalFactor2 — pieces per box, at sync time.',
    )
    sal_pack_un = models.DecimalField(
        max_digits=19, decimal_places=6, null=True, blank=True,
        help_text='OITM.SalPackUn — volume per piece, at sync time.',
    )

    order_type = models.CharField(
        max_length=1, choices=SapOrderType.choices, default=SapOrderType.STANDARD,
    )
    post_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    start_date = models.DateField(null=True, blank=True)

    #: `OWOR.U_BATCH_NO` / `U_MFG` / `U_EXP_DATE` — user-defined fields the
    #: factory fills in. Carried so an approver sees what they are approving.
    batch_no = models.CharField(max_length=50, blank=True, default='')
    mfg_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)

    #: Who raised it in SAP. Text + the raw `OWOR.UserSign`, never an FK.
    #: `sap_user_sign` is kept because the SAP release gate exempts one
    #: specific id (`UserSign != 33`), and an approval that was never going to
    #: be enforced should be visible as such rather than silently equivalent
    #: to one that was. See PRDO_DESIGN.md §7.2.
    sap_created_by = models.CharField(max_length=100, blank=True, default='')
    sap_user_sign = models.SmallIntegerField(null=True, blank=True)

    remarks = models.TextField(blank=True, default='')

    #: Last seen `OWOR.Status`. The sync refreshes it, and a value other than
    #: `P` on a pending flow is what retires the request — see
    #: `services.sync.reconcile`.
    sap_status = models.CharField(
        max_length=1, choices=SapOrderStatus.choices,
        default=SapOrderStatus.PLANNED, db_index=True,
    )
    synced_at = models.DateTimeField()

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = _t('production_order')
        ordering = ['-created_at']
        verbose_name = 'Production Order'
        constraints = [
            # THE important one. DocEntry sequences run per company, so
            # DocEntry alone is not a key. JSAP had no company column at all.
            models.UniqueConstraint(
                fields=['company', 'sap_doc_entry'],
                name='production_order_sap_uq',
            ),
            models.CheckConstraint(
                condition=Q(company__in=COMPANY_CODES),
                name='production_order_company_valid',
            ),
            models.CheckConstraint(
                condition=Q(order_type__in=[c for c, _ in SapOrderType.choices]),
                name='production_order_type_valid',
            ),
            models.CheckConstraint(
                condition=Q(sap_status__in=[c for c, _ in SapOrderStatus.choices]),
                name='production_order_sap_status_valid',
            ),
            models.CheckConstraint(
                condition=Q(planned_qty__gt=0),
                name='production_order_qty_positive',
            ),
        ]
        indexes = [
            models.Index(fields=['company', 'sap_status'], name='prdo_company_status_idx'),
            models.Index(fields=['item_code'], name='prdo_item_idx'),
        ]

    def __str__(self):
        return f'{self.company} PO {self.sap_doc_num or self.sap_doc_entry} — {self.item_code}'

    @property
    def planned_boxes(self):
        """Pieces / pack size, or None when the pack size was not captured.

        Derived on read rather than stored: a stored box count that disagrees
        with `planned_qty` would be a second, quieter source of truth.
        """
        if not self.sal_factor2:
            return None
        return self.planned_qty / self.sal_factor2

    @property
    def planned_litres(self):
        if self.sal_pack_un is None:
            return None
        return self.planned_qty * self.sal_pack_un


class ProductionOrderFlow(models.Model):
    """Where one request currently is, and what the SAP write-back said.

    ONE ROW PER REQUEST, and no task table underneath it. The stage it waits
    at is here; the stages it has passed are in the action log; the stages
    ahead are configuration the engine already stores. A row per stage per
    request restated all three and had to be kept in step with them.

    WHY `current_user` IS NOT THE AUTHORITY
    ---------------------------------------
    `current_stage` is. `current_user` is written alongside it so a list can
    be rendered and filtered without resolving every row through the engine,
    but "who may act now?" is always answered by
    `workflow.services.assignments.get_stage_assignment(current_stage_id)`,
    which applies today's replacements. A stored user consulted as the
    authority would freeze both stage reassignment and stand-in cover.
    """

    production_order = models.OneToOneField(
        ProductionOrder, on_delete=models.CASCADE, related_name='flow',
    )
    status = models.CharField(
        max_length=10, choices=FlowStatus.choices, default=FlowStatus.PENDING,
        db_index=True,
    )

    #: NULL until the final approval writes to SAP.
    sap_status = models.CharField(
        max_length=10, choices=SapWriteStatus.choices, null=True, blank=True,
    )
    #: Exactly what was written to the SAP-side approval table. Kept because
    #: it is not reproducible from the request afterwards, and because a
    #: generic "failed to post" message is what made the JSAP equivalent
    #: undiagnosable.
    sap_payload = models.JSONField(null=True, blank=True, default=None)
    #: What SAP actually said — the driver's confirmation, or the database
    #: error verbatim. Never replaced by an application message.
    sap_status_text = models.TextField(blank=True, default='')

    #: Denormalised from `current_stage`. See the class docstring.
    current_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    #: The workflow the engine selected. PROTECT — deleting configuration that
    #: explains a past decision would silently rewrite history.
    workflow = models.ForeignKey(
        'workflow.Workflow', on_delete=models.PROTECT, related_name='+',
    )
    #: The engine stage awaiting a decision; NULL once the flow is finished.
    current_stage = models.ForeignKey(
        'workflow.WorkflowStage', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+', db_column='current_stage',
    )
    #: Stage count of the chosen workflow AT SUBMISSION, so "stage 2 of 3"
    #: stays correct after a stage is added or retired. A count, never a name.
    total_stage = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = _t('production_order_flow')
        ordering = ['-created_at']
        verbose_name = 'Production Order Flow'
        indexes = [
            models.Index(fields=['status', 'current_user'], name='prdo_flow_queue_idx'),
            models.Index(fields=['current_stage'], name='prdo_flow_stage_idx'),
            models.Index(fields=['workflow'], name='prdo_flow_workflow_idx'),
        ]

    def __str__(self):
        return f'{self.production_order_id} [{self.status}]'

    @property
    def is_open(self):
        return self.status == FlowStatus.PENDING


class ProductionOrderActionLog(models.Model):
    """Append-only history. Nothing in this module updates or deletes a row.

    Stage NAME and SEQUENCE are deliberately not duplicated here — they are
    resolved through `stage` so a renamed stage reads correctly in history
    too. Order by `acted_at, id`, never by a stored sequence.
    """

    production_order = models.ForeignKey(
        ProductionOrder, on_delete=models.CASCADE, related_name='action_logs',
    )
    action = models.CharField(max_length=10, choices=LogAction.choices)
    #: NULL for SYNC and OBSOLETE — neither is performed by an OMS user.
    acted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    #: The stage being decided. NULL on SYNC (nothing decided yet) and on
    #: OBSOLETE (the flow was retired, not judged).
    stage = models.ForeignKey(
        'workflow.WorkflowStage', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='+',
    )
    remarks = models.TextField(blank=True, default='')
    #: Changed fields, on a refresh that actually altered the snapshot. Same
    #: shape as `payments.PaymentStatusHistory.change_data`:
    #: {"planned_qty": {"old": "100.000000", "new": "120.000000"}}
    #: NULL when nothing tracked changed, so a row never claims an edit it
    #: cannot describe.
    action_data = models.JSONField(null=True, blank=True, default=None)
    acted_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = _t('production_order_action_logs')
        ordering = ['acted_at', 'id']
        verbose_name = 'Production Order Action Log'
        indexes = [
            models.Index(fields=['production_order', 'acted_at'],
                         name='prdo_log_order_idx'),
        ]

    def __str__(self):
        return f'{self.action} on {self.production_order_id}'
