"""`apply_action` — the single entry point for every stage transition.

Plan item 0.3, and the largest untested decision surface left in the project:
`tracker` is 3,905 lines and 15 models, and every movement of every invoice —
advance, return, hold, debit, rejection — goes through this one function. It
had no test.

These are CHARACTERISATION tests. They pin what the code does today, including
the parts that look surprising, so a later refactor has to be deliberate about
changing them. Where behaviour is genuinely subtle the docstring says why it is
the way it is rather than asserting that it is right.

The stage definitions are imported from the seed command rather than written
out here. A copy would drift, and then these tests would be describing a flow
the application does not have.

Run with::

    python manage.py test tracker --settings=OMS.test_settings
"""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.utils import timezone

from users.models import User

from .management.commands.seed_tracker import STAGES
from .models import (
    AlertMute, Branch, Category, GstRate, GstType, Invoice, InvoiceMode,
    PaymentDetail, Stage, StageEvent, Unit, UserStageAccess,
)
from .services import (
    SKIPPED_STATUS, _days_between, _open_event, alert_mute_map, apply_action,
    auto_advance_to_payment, clear_alert_mute, create_invoice, days_at_stage,
    fast_track, full_hold_invoice_ids, sap_saved_remarks, set_alert_mute,
    stage_route, sync_sap_saved,
)


class TrackerFlowTestCase(TestCase):
    """Builds the real eight-stage flow plus the lookups an invoice needs."""

    @classmethod
    def setUpTestData(cls):
        for spec in STAGES:
            Stage.objects.update_or_create(code=spec['code'], defaults=spec)
        cls.stages = {s.code: s for s in Stage.objects.all()}

        cls.gst_type = GstType.objects.create(name='IGST')
        cls.gst_rate = GstRate.objects.create(label='18%', rate=Decimal('18'))
        cls.unit = Unit.objects.create(name='Unit A')
        cls.branch = Branch.objects.create(name='Delhi')
        cls.mode = InvoiceMode.objects.create(name='Regular')
        cls.oil = Category.objects.create(name='Oil')
        cls.transport = Category.objects.create(name='Transport')
        cls.rm_pm = Category.objects.create(name='RM-PM')

        cls.clerk = User.objects.create_user(username='tk-clerk', password='pw',
                                             name='Clerk')
        cls.other = User.objects.create_user(username='tk-other', password='pw',
                                             name='Other')

    def grant(self, user, *codes):
        for code in codes:
            UserStageAccess.objects.get_or_create(
                user=user, stage=self.stages[code], defaults={'is_active': True})

    def make_invoice(self, category=None, created_by=None, number='INV-1'):
        return create_invoice(
            created_by=created_by or self.clerk,
            invoice_date=date(2026, 8, 1),
            effective_month=date(2026, 8, 1),
            party_name='ACME',
            invoice_number=number,
            taxable_value=Decimal('1000.00'),
            invoice_value=Decimal('1180.00'),
            gst_type=self.gst_type,
            gst_rate=self.gst_rate,
            category=category or self.oil,
            unit=self.unit,
            branch=self.branch,
            mode=self.mode,
        )

    def move_to(self, invoice, code, user=None):
        """Walk an invoice up its route to `code`, granting access as needed."""
        user = user or self.clerk
        guard = 0
        while invoice.current_stage.code != code:
            guard += 1
            self.assertLess(guard, 20, 'route walk did not terminate')
            self.grant(user, invoice.current_stage.code)
            stage = invoice.current_stage
            status = stage.status_choices[0] if stage.requires_status else ''
            invoice = apply_action(invoice=invoice, user=user,
                                   stage_status=status, remarks='ok')
        self.grant(user, code)
        return invoice


class RouteTests(TrackerFlowTestCase):
    """Not every invoice visits every desk."""

    def test_a_non_transport_invoice_skips_the_bilty_desk(self):
        codes = [s.code for s in stage_route(self.make_invoice(self.oil))]
        self.assertNotIn('bilty_grpo', codes)
        self.assertEqual(codes[:2], ['entry', 'pre_audit'])

    def test_a_transport_invoice_visits_it(self):
        codes = [s.code for s in stage_route(self.make_invoice(self.transport))]
        self.assertEqual(codes[:3], ['entry', 'bilty_grpo', 'pre_audit'])

    def test_advancing_follows_the_route_not_the_stage_order(self):
        """`_route_neighbour` walks the invoice's OWN route, so an oil invoice
        at entry goes to pre_audit (order 3), not to bilty_grpo (order 2)."""
        invoice = self.make_invoice(self.oil)
        self.grant(self.clerk, 'entry')
        invoice = apply_action(invoice=invoice, user=self.clerk)
        self.assertEqual(invoice.current_stage.code, 'pre_audit')


class PermissionTests(TrackerFlowTestCase):

    def test_a_user_not_mapped_to_the_stage_is_refused(self):
        invoice = self.make_invoice()
        with self.assertRaises(PermissionDenied):
            apply_action(invoice=invoice, user=self.other)

    def test_the_creator_may_act_at_the_entry_stage_without_a_mapping(self):
        """Entry is the one stage where authorship substitutes for a mapping —
        otherwise whoever keys an invoice in could not submit it."""
        invoice = self.make_invoice(created_by=self.other)
        moved = apply_action(invoice=invoice, user=self.other)
        self.assertEqual(moved.current_stage.code, 'pre_audit')

    def test_authorship_does_not_carry_past_entry(self):
        invoice = self.make_invoice(created_by=self.other)
        invoice = apply_action(invoice=invoice, user=self.other)
        with self.assertRaises(PermissionDenied):
            apply_action(invoice=invoice, user=self.other, stage_status='OK')

    def test_the_system_may_always_act(self):
        """`user=None` is the JSAP sync mirroring a decision made in JSAP.
        There is no person to map to a stage, and the event is logged with
        acted_by NULL."""
        invoice = self.make_invoice()
        moved = apply_action(invoice=invoice, user=None)
        self.assertEqual(moved.current_stage.code, 'pre_audit')

    def test_a_completed_invoice_cannot_be_moved(self):
        invoice = self.make_invoice()
        invoice.status = Invoice.Status.COMPLETED
        invoice.save(update_fields=['status'])
        self.grant(self.clerk, 'entry')
        with self.assertRaises(ValidationError):
            apply_action(invoice=invoice, user=self.clerk)


class StatusRequirementTests(TrackerFlowTestCase):

    def test_a_stage_that_requires_a_status_refuses_a_blank_one(self):
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        with self.assertRaises(ValidationError):
            apply_action(invoice=invoice, user=self.clerk)

    def test_a_status_outside_the_stage_choices_is_refused(self):
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        with self.assertRaises(ValidationError):
            apply_action(invoice=invoice, user=self.clerk, stage_status='APPROVED')

    def test_a_stage_without_choices_advances_on_no_status(self):
        invoice = self.make_invoice()
        self.grant(self.clerk, 'entry')
        self.assertEqual(
            apply_action(invoice=invoice, user=self.clerk).current_stage.code,
            'pre_audit')


class ReturnTests(TrackerFlowTestCase):

    def test_a_return_needs_remarks(self):
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        with self.assertRaises(ValidationError):
            apply_action(invoice=invoice, user=self.clerk,
                         stage_status='RETURN', remarks='')

    def test_a_return_moves_the_invoice_back_along_its_route(self):
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        moved = apply_action(invoice=invoice, user=self.clerk,
                             stage_status='RETURN', remarks='wrong party')
        self.assertEqual(moved.current_stage.code, 'entry')

    def test_the_entry_stage_cannot_return(self):
        """`entry` is seeded with can_return=False — there is nowhere behind
        it, and the check is on the stage rather than the route position."""
        invoice = self.make_invoice()
        self.grant(self.clerk, 'entry')
        with self.assertRaises(ValidationError):
            apply_action(invoice=invoice, user=self.clerk, action='RETURN',
                         remarks='nope')

    def test_an_explicit_return_action_overrides_an_advancing_status(self):
        """A caller may send status=OK and action=RETURN. The action wins, and
        still demands remarks, which the OK path would not have."""
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        moved = apply_action(invoice=invoice, user=self.clerk, action='RETURN',
                             stage_status='OK', remarks='send it back')
        self.assertEqual(moved.current_stage.code, 'entry')


class RejectionTests(TrackerFlowTestCase):
    """REJECTED behaves differently depending on whether a reason is given."""

    def test_a_rejection_without_remarks_is_parked_not_returned(self):
        """It stays at the stage with `rejection_pending` set, which is what
        puts it on the Rejected tab. The remarks come later and turn it into a
        real return."""
        invoice = self.move_to(self.make_invoice(), 'sap_approval')
        moved = apply_action(invoice=invoice, user=self.clerk,
                             stage_status='REJECTED', remarks='')
        self.assertEqual(moved.current_stage.code, 'sap_approval')
        self.assertTrue(moved.rejection_pending)

    def test_a_rejection_with_remarks_returns_the_invoice(self):
        invoice = self.move_to(self.make_invoice(), 'sap_approval')
        moved = apply_action(invoice=invoice, user=self.clerk,
                             stage_status='REJECTED', remarks='wrong GST')
        self.assertEqual(moved.current_stage.code, 'data_entry')

    def test_a_pending_rejection_is_cleared_when_the_invoice_moves(self):
        invoice = self.move_to(self.make_invoice(), 'sap_approval')
        apply_action(invoice=invoice, user=self.clerk,
                     stage_status='REJECTED', remarks='')
        moved = apply_action(invoice=invoice, user=self.clerk,
                             stage_status='APPROVED')
        self.assertFalse(moved.rejection_pending)


class HoldTests(TrackerFlowTestCase):

    def test_a_full_hold_keeps_the_invoice_where_it_is(self):
        """The dwell clock keeps running deliberately: a held invoice is still
        this desk's problem, so the stuck-invoice alert should still fire."""
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        before = invoice.current_stage_entered_at
        moved = apply_action(invoice=invoice, user=self.clerk,
                             stage_status='HOLD', remarks='awaiting proof')
        self.assertEqual(moved.current_stage.code, 'pre_audit')
        self.assertEqual(moved.current_stage_entered_at, before)

    def test_a_full_hold_still_needs_a_reason(self):
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        with self.assertRaises(ValidationError):
            apply_action(invoice=invoice, user=self.clerk, stage_status='HOLD')

    def test_a_partial_hold_advances_and_records_the_amount(self):
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        moved = apply_action(invoice=invoice, user=self.clerk,
                             stage_status='HOLD', hold_type='PARTIAL',
                             amount='250.00', remarks='short delivery')
        self.assertEqual(moved.current_stage.code, 'data_entry')
        self.assertEqual(moved.hold_amount, Decimal('250.00'))

    def test_a_partial_hold_without_an_amount_is_refused(self):
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        with self.assertRaises(ValidationError):
            apply_action(invoice=invoice, user=self.clerk, stage_status='HOLD',
                         hold_type='PARTIAL', remarks='short')

    def test_rm_pm_categories_may_hold_without_an_amount(self):
        """A deliberate exception: RM-PM style invoices are held for reasons
        that have no rupee value attached."""
        invoice = self.move_to(self.make_invoice(self.rm_pm), 'pre_audit')
        moved = apply_action(invoice=invoice, user=self.clerk,
                             stage_status='HOLD', hold_type='PARTIAL',
                             remarks='qc pending')
        self.assertEqual(moved.current_stage.code, 'data_entry')


class DebitTests(TrackerFlowTestCase):

    def test_a_debit_needs_an_amount(self):
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        with self.assertRaises(ValidationError):
            apply_action(invoice=invoice, user=self.clerk,
                         stage_status='DEBIT', remarks='damaged')

    def test_a_debit_advances_and_is_kept_on_the_invoice(self):
        """The debit is permanent: every later stage and the payment maths use
        the net value, so it is stored on the invoice rather than only on the
        event."""
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        moved = apply_action(invoice=invoice, user=self.clerk,
                             stage_status='DEBIT', amount='75.50',
                             remarks='damaged carton')
        self.assertEqual(moved.current_stage.code, 'data_entry')
        self.assertEqual(moved.debit_amount, Decimal('75.50'))

    def test_a_non_numeric_amount_is_refused(self):
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        with self.assertRaises(ValidationError):
            apply_action(invoice=invoice, user=self.clerk, stage_status='DEBIT',
                         amount='lots', remarks='x')


class LockingTests(TrackerFlowTestCase):
    """Editing is allowed at the entry desk and nowhere else."""

    def test_advancing_out_of_entry_locks_the_invoice(self):
        invoice = self.make_invoice()
        self.assertFalse(invoice.is_locked)
        self.grant(self.clerk, 'entry')
        self.assertTrue(apply_action(invoice=invoice, user=self.clerk).is_locked)

    def test_returning_to_entry_unlocks_it_again(self):
        """Otherwise an invoice bounced back for correction could not be
        corrected."""
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        moved = apply_action(invoice=invoice, user=self.clerk,
                             stage_status='RETURN', remarks='fix the GSTIN')
        self.assertEqual(moved.current_stage.code, 'entry')
        self.assertFalse(moved.is_locked)


class TerminalStageTests(TrackerFlowTestCase):

    def test_arriving_at_payment_creates_the_payment_row(self):
        invoice = self.move_to(self.make_invoice(), 'payment')
        self.assertTrue(PaymentDetail.objects.filter(invoice=invoice).exists())

    def test_nothing_advances_past_the_terminal_stage(self):
        invoice = self.move_to(self.make_invoice(), 'payment')
        with self.assertRaises(ValidationError):
            apply_action(invoice=invoice, user=self.clerk)


class StageEventTests(TrackerFlowTestCase):
    """Every movement closes one visit row and opens the next. The audit trail
    and every dwell-time report are built from these."""

    def test_advancing_closes_the_open_visit_and_opens_a_new_one(self):
        invoice = self.make_invoice()
        self.grant(self.clerk, 'entry')
        invoice = apply_action(invoice=invoice, user=self.clerk)

        closed = StageEvent.objects.get(invoice=invoice,
                                        stage=self.stages['entry'],
                                        exited_at__isnull=False)
        self.assertEqual(closed.event_type, StageEvent.EventType.ADVANCE)
        self.assertIsNotNone(closed.days_spent)

        open_now = StageEvent.objects.filter(invoice=invoice,
                                             stage=invoice.current_stage,
                                             exited_at__isnull=True)
        self.assertEqual(open_now.count(), 1)

    def test_a_return_is_recorded_as_a_return(self):
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        apply_action(invoice=invoice, user=self.clerk,
                     stage_status='RETURN', remarks='back')
        closed = StageEvent.objects.filter(stage=self.stages['pre_audit'],
                                           exited_at__isnull=False).latest('id')
        self.assertEqual(closed.event_type, StageEvent.EventType.RETURN)

    def test_a_hold_adds_a_note_without_closing_the_visit(self):
        """The hold is recorded as its own row and the invoice does not move."""
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        before = StageEvent.objects.filter(invoice=invoice).count()
        apply_action(invoice=invoice, user=self.clerk,
                     stage_status='HOLD', remarks='wait')
        self.assertEqual(StageEvent.objects.filter(invoice=invoice).count(),
                         before + 1)

    def test_a_hold_note_is_distinguishable_from_the_open_visit(self):
        """These two tests were written as CHARACTERISATION of a defect and
        now pin its fix. What they described:

        The HOLD branch wrote its note as a RECEIVE with `entered_at` copied
        from the visit and no `exited_at`, so the stage held two rows that both
        looked like an open visit and tied on the only column anything ordered
        by. `_open_event` picks with `.order_by('-entered_at').first()`, so
        which one a later advance closed was left to the database — and the
        other was stranded open at a stage the invoice had left, where every
        queue count, stuck-invoice alert and dwell report still saw it.

        The fix is `EventType.NOTE`. `exited_at` could not be the discriminator:
        `tracker/views.py` reads `exited_at is None` on a rejection note to mean
        "awaiting the written reason", so closing notes would have silently
        cleared every pending rejection in the SAP/JSAP two-step.
        """
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        apply_action(invoice=invoice, user=self.clerk,
                     stage_status='HOLD', remarks='wait')

        rows = StageEvent.objects.filter(
            invoice=invoice, stage=self.stages['pre_audit'],
            exited_at__isnull=True)
        # Both rows still have no `exited_at` — that is deliberate and load
        # bearing. What changed is that they no longer look alike.
        self.assertEqual(rows.count(), 2)
        self.assertEqual({r.event_type for r in rows},
                         {StageEvent.EventType.RECEIVE, StageEvent.EventType.NOTE})

    def test_the_open_visit_is_picked_regardless_of_the_note(self):
        """The tie is still there — the note copies the visit's `entered_at` —
        so what makes the pick safe is the type filter, not luck."""
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        visit_before = _open_event(invoice)
        apply_action(invoice=invoice, user=self.clerk,
                     stage_status='HOLD', remarks='wait')
        self.assertEqual(_open_event(invoice).pk, visit_before.pk)

    def test_no_row_is_stranded_after_the_invoice_leaves_the_stage(self):
        """The consequence the fix removes. Previously an invoice that was held
        once left behind a row reading "still at pre_audit" for ever."""
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        apply_action(invoice=invoice, user=self.clerk,
                     stage_status='HOLD', remarks='wait')
        moved = apply_action(invoice=invoice, user=self.clerk, stage_status='OK')
        self.assertEqual(moved.current_stage.code, 'data_entry')

        # The visit closed, so nothing reads as an open VISIT at a stage the
        # invoice has left.
        stranded = StageEvent.objects.filter(
            invoice=invoice, stage=self.stages['pre_audit'],
            exited_at__isnull=True).exclude(
                event_type=StageEvent.EventType.NOTE)
        self.assertEqual(stranded.count(), 0)

        # The note survives, unexited, because it is a record of what happened
        # rather than a claim about where the invoice is.
        note = StageEvent.objects.get(
            invoice=invoice, stage=self.stages['pre_audit'],
            event_type=StageEvent.EventType.NOTE)
        self.assertEqual(note.stage_status, 'HOLD')

    def test_a_second_open_visit_at_one_stage_is_rejected_by_the_database(self):
        """The constraint, actually fired.

        Adding a constraint and never testing it is how you get a constraint
        that does not do what its name says — the index exists, the migration
        applied, and nothing ever tried to violate it. `apply_action` cannot
        produce this state, which is the point: the constraint is there for the
        code that does NOT go through `apply_action`.
        """
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        stage = self.stages['pre_audit']
        self.assertIsNotNone(_open_event(invoice))

        with self.assertRaises(IntegrityError):
            StageEvent.objects.create(
                invoice=invoice, stage=stage,
                event_type=StageEvent.EventType.RECEIVE,
                entered_at=timezone.now(),
            )

    def test_several_notes_at_one_stage_are_allowed(self):
        """The other half of the predicate. A stage can legitimately carry a
        hold and then a rejection awaiting its reason, both with `exited_at`
        NULL — a constraint that forbade that would break the flow it is
        meant to protect."""
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        stage = self.stages['pre_audit']
        for status in ('HOLD', 'REJECTED'):
            StageEvent.objects.create(
                invoice=invoice, stage=stage,
                event_type=StageEvent.EventType.NOTE,
                stage_status=status, entered_at=timezone.now(),
            )
        self.assertEqual(
            StageEvent.objects.filter(
                invoice=invoice, stage=stage,
                event_type=StageEvent.EventType.NOTE).count(), 2)

    def test_the_same_stage_can_be_revisited_after_it_closes(self):
        """A RETURN sends an invoice back and it comes through again. The
        constraint is on OPEN visits only, so a closed one must not block the
        next — this is the regression that a plain `unique_together` would
        have caused."""
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        stage = self.stages['pre_audit']
        visit = _open_event(invoice)
        visit.exited_at = timezone.now()
        visit.save()

        StageEvent.objects.create(
            invoice=invoice, stage=stage,
            event_type=StageEvent.EventType.RECEIVE,
            entered_at=timezone.now(),
        )
        self.assertEqual(
            StageEvent.objects.filter(invoice=invoice, stage=stage).count(), 2)

    def test_a_rejection_note_keeps_its_null_exited_at(self):
        """`tracker/views.py` reads exactly this to mean "awaiting the written
        reason". The fix had to leave it alone, and this is the guard that says
        so — a later tidy-up that closes notes would break the SAP/JSAP
        two-step rejection silently."""
        invoice = self.move_to(self.make_invoice(), 'sap_approval')
        apply_action(invoice=invoice, user=self.clerk, stage_status='REJECTED')
        note = StageEvent.objects.filter(
            invoice=invoice, event_type=StageEvent.EventType.NOTE).first()
        self.assertIsNotNone(note)
        self.assertIsNone(note.exited_at)


class TransportApprovalDetourTests(TrackerFlowTestCase):
    """Transport Approval is a DETOUR off Pre-Audit, not a step in the line.

        Pre-Audit --(1st advance, Transport only)--> Transport Approval
        Transport Approval --APPROVED or REJECTED--> Pre-Audit
        Pre-Audit --(advance, once approved)-------> Data Entry

    So a Transport invoice passes Pre-Audit twice and only the SECOND advance
    continues down the line. See `services._detour_target`.
    """

    def _advance(self, invoice, status='', remarks='ok'):
        self.grant(self.clerk, invoice.current_stage.code)
        return apply_action(invoice=invoice, user=self.clerk,
                            stage_status=status, remarks=remarks)

    def test_the_desk_is_not_on_anyones_linear_route(self):
        """If it were, `_route_neighbour` would walk every invoice into it."""
        for n, category in enumerate((self.oil, self.transport)):
            invoice = self.make_invoice(category, number=f'ROUTE-{n}')
            codes = [s.code for s in stage_route(invoice)]
            self.assertNotIn('transport_approval', codes)

    def test_a_transport_invoice_detours_on_the_first_pre_audit_advance(self):
        invoice = self.move_to(self.make_invoice(self.transport), 'pre_audit')
        invoice = self._advance(invoice, 'OK')
        self.assertEqual(invoice.current_stage.code, 'transport_approval')

    def test_a_non_transport_invoice_goes_straight_to_data_entry(self):
        invoice = self.move_to(self.make_invoice(self.oil), 'pre_audit')
        invoice = self._advance(invoice, 'OK')
        self.assertEqual(invoice.current_stage.code, 'data_entry')

    def test_approval_hands_it_back_to_pre_audit_then_on_to_data_entry(self):
        invoice = self.move_to(self.make_invoice(self.transport), 'pre_audit')
        invoice = self._advance(invoice, 'OK')
        self.assertEqual(invoice.current_stage.code, 'transport_approval')

        invoice = self._advance(invoice, 'APPROVED')
        self.assertEqual(invoice.current_stage.code, 'pre_audit')

        # The SECOND Pre-Audit advance is the one that continues down the line.
        invoice = self._advance(invoice, 'OK')
        self.assertEqual(invoice.current_stage.code, 'data_entry')

    def test_rejection_hands_it_back_and_it_must_go_round_again(self):
        """The point of the gate. Were it merely "has visited the desk", one
        rejection would let the invoice slip past into Data Entry — the exact
        approval it was just denied."""
        invoice = self.move_to(self.make_invoice(self.transport), 'pre_audit')
        invoice = self._advance(invoice, 'OK')
        invoice = self._advance(invoice, 'REJECTED', remarks='wrong bilty')
        self.assertEqual(invoice.current_stage.code, 'pre_audit')

        invoice = self._advance(invoice, 'OK')
        self.assertEqual(invoice.current_stage.code, 'transport_approval')

        # ...and once actually approved, it proceeds.
        invoice = self._advance(invoice, 'APPROVED')
        invoice = self._advance(invoice, 'OK')
        self.assertEqual(invoice.current_stage.code, 'data_entry')

    def test_a_pre_audit_return_still_walks_back_down_the_line(self):
        """Only an ADVANCE out of Pre-Audit is diverted."""
        invoice = self.move_to(self.make_invoice(self.transport), 'pre_audit')
        # Pre-Audit requires a status, so the send-back carries RETURN.
        invoice = apply_action(invoice=invoice, user=self.clerk, action='RETURN',
                               stage_status='RETURN', remarks='send it back')
        self.assertEqual(invoice.current_stage.code, 'bilty_grpo')


class FastTrackTests(TrackerFlowTestCase):
    """`fast_track` — Invoice Entry straight to SAP Approval.

    The point of interest is what happens to the desks it jumps: they are
    recorded as zero-day SKIPPED visits rather than omitted, so reports built
    from `StageEvent` still see every invoice pass through every desk on its
    route and an audit can tell a bypass from a same-day approval.
    """

    def test_it_lands_at_sap_approval(self):
        invoice = self.make_invoice()
        self.grant(self.clerk, 'entry')
        invoice = fast_track(invoice, self.clerk, 'urgent — MD approved')
        self.assertEqual(invoice.current_stage.code, 'sap_approval')

    def test_the_skipped_desks_are_recorded_not_omitted(self):
        invoice = self.make_invoice()
        self.grant(self.clerk, 'entry')
        fast_track(invoice, self.clerk, 'urgent')

        skipped = StageEvent.objects.filter(
            invoice=invoice, stage_status=SKIPPED_STATUS)
        codes = sorted(e.stage.code for e in skipped)
        # Non-transport, so Bilty/GRPO is not on its route to begin with.
        self.assertEqual(codes, ['data_entry', 'pre_audit'])
        for ev in skipped:
            self.assertEqual(ev.days_spent, Decimal('0.00'))
            self.assertEqual(ev.entered_at, ev.exited_at)
            self.assertEqual(ev.acted_by, self.clerk)

    def test_a_transport_invoice_also_skips_bilty(self):
        invoice = self.make_invoice(self.transport)
        self.grant(self.clerk, 'entry')
        fast_track(invoice, self.clerk, 'urgent')
        codes = sorted(
            e.stage.code for e in StageEvent.objects.filter(
                invoice=invoice, stage_status=SKIPPED_STATUS))
        self.assertEqual(codes, ['bilty_grpo', 'data_entry', 'pre_audit'])

    def test_a_reason_is_mandatory(self):
        """It bypasses Pre-Audit, which is where holds and debits are captured —
        so the only record of why is the remark."""
        invoice = self.make_invoice()
        self.grant(self.clerk, 'entry')
        with self.assertRaises(ValidationError):
            fast_track(invoice, self.clerk, '   ')

    def test_it_refuses_from_any_stage_but_entry(self):
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        with self.assertRaises(ValidationError):
            fast_track(invoice, self.clerk, 'too late')

    def test_it_locks_the_invoice(self):
        """Same rule as a normal advance out of entry: once it leaves, the entry
        desk can no longer edit it."""
        invoice = self.make_invoice()
        self.grant(self.clerk, 'entry')
        invoice = fast_track(invoice, self.clerk, 'urgent')
        self.assertTrue(invoice.is_locked)

    def test_the_entry_visit_keeps_its_real_dwell_time(self):
        """Only the BYPASSED desks are zero-day. Entry genuinely held it."""
        invoice = self.make_invoice()
        self.grant(self.clerk, 'entry')
        fast_track(invoice, self.clerk, 'urgent')
        entry_visit = StageEvent.objects.get(
            invoice=invoice, stage__code='entry', exited_at__isnull=False)
        self.assertEqual(entry_visit.stage_status, '')
        self.assertIsNotNone(entry_visit.days_spent)


class FullHoldQueueTests(TrackerFlowTestCase):
    """A FULL hold belongs in the Hold tab only — `full_hold_invoice_ids` is what
    keeps it out of Current."""

    def _hold(self, invoice):
        return apply_action(invoice=invoice, user=self.clerk,
                            stage_status='HOLD', hold_type='FULL',
                            remarks='waiting on vendor')

    def test_a_full_held_invoice_is_flagged(self):
        invoice = self._hold(self.move_to(self.make_invoice(), 'pre_audit'))
        self.assertEqual(full_hold_invoice_ids([invoice.id]), {invoice.id})

    def test_an_ordinary_invoice_is_not(self):
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        self.assertEqual(full_hold_invoice_ids([invoice.id]), set())

    def test_a_partial_hold_is_not_flagged(self):
        """A partial hold ADVANCES the invoice, so it is never sitting at the
        desk that recorded it."""
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        invoice = apply_action(invoice=invoice, user=self.clerk,
                               stage_status='HOLD', hold_type='PARTIAL',
                               amount=Decimal('100.00'), remarks='part held')
        self.assertEqual(full_hold_invoice_ids([invoice.id]), set())

    def test_the_flag_does_not_survive_the_invoice_moving_on(self):
        """The hold is scoped to the VISIT. Once released and advanced, the
        invoice is a normal row at its new desk — it must not stay hidden from
        Current forever because of a hold two desks ago."""
        invoice = self._hold(self.move_to(self.make_invoice(), 'pre_audit'))
        invoice = apply_action(invoice=invoice, user=self.clerk,
                               stage_status='OK', remarks='released')
        self.assertEqual(full_hold_invoice_ids([invoice.id]), set())

    def test_empty_input_costs_no_query(self):
        with self.assertNumQueries(0):
            self.assertEqual(full_hold_invoice_ids([]), set())


# IST is UTC+5:30 and has no DST, so a fixed offset is exact here and keeps the
# fixtures readable: the numbers below are wall-clock times in the office.
IST = dt_timezone(timedelta(hours=5, minutes=30))


def ist(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=IST)


@override_settings(TRACKER_BUSINESS_TIMEZONE='Asia/Kolkata',
                   TRACKER_OFF_WEEKDAYS=(6,))
class SundayIsNotCountedTests(TestCase):
    """The dwell clock stops on Sunday, because the office does.

    2026-09-13 is a Sunday; 09-12 is the Saturday before it and 09-14 the
    Monday after. Every fixture below is anchored to that weekend.
    """

    def test_a_weekend_costs_one_day_not_two(self):
        self.assertEqual(_days_between(ist(2026, 9, 12, 10), ist(2026, 9, 14, 10)),
                         Decimal('1.00'))

    def test_a_span_wholly_inside_sunday_is_zero(self):
        self.assertEqual(_days_between(ist(2026, 9, 13, 2), ist(2026, 9, 13, 23)),
                         Decimal('0.00'))

    def test_only_the_sunday_part_is_dropped(self):
        """Saturday 18:00 to Monday 06:00 is 36 hours, 24 of them Sunday."""
        self.assertEqual(_days_between(ist(2026, 9, 12, 18), ist(2026, 9, 14, 6)),
                         Decimal('0.50'))

    def test_three_weeks_lose_three_sundays(self):
        self.assertEqual(
            _days_between(ist(2026, 9, 12, 10), ist(2026, 10, 3, 10)),
            Decimal('18.00'))

    def test_an_ordinary_weekday_span_is_unchanged(self):
        self.assertEqual(_days_between(ist(2026, 9, 14, 9), ist(2026, 9, 16, 9)),
                         Decimal('2.00'))

    def test_the_sunday_boundary_is_the_office_day_not_the_utc_one(self):
        """Sunday 00:30 IST is still SATURDAY in UTC (19:00 the day before).

        This is the whole reason for TRACKER_BUSINESS_TIMEZONE: on the
        project's UTC calendar this hour would have been charged as working
        time, and the equivalent hour of Monday morning written off instead.
        """
        self.assertEqual(_days_between(ist(2026, 9, 13, 0), ist(2026, 9, 13, 1)),
                         Decimal('0.00'))

    @override_settings(TRACKER_OFF_WEEKDAYS=())
    def test_no_off_days_configured_restores_plain_elapsed_time(self):
        self.assertEqual(_days_between(ist(2026, 9, 12, 10), ist(2026, 9, 14, 10)),
                         Decimal('2.00'))

    def test_a_backwards_span_does_not_go_negative(self):
        self.assertEqual(_days_between(ist(2026, 9, 14, 10), ist(2026, 9, 12, 10)),
                         Decimal('0.00'))


@override_settings(TRACKER_BUSINESS_TIMEZONE='Asia/Kolkata',
                   TRACKER_OFF_WEEKDAYS=(6,))
class SundayAndStuckDetectionTests(TrackerFlowTestCase):
    """The same rule reaches the ageing of a real invoice, not just the helper."""

    def test_an_invoice_parked_over_the_weekend_ages_by_one_day(self):
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        Invoice.objects.filter(pk=invoice.pk).update(
            current_stage_entered_at=ist(2026, 9, 12, 10))
        invoice.refresh_from_db()
        self.assertEqual(days_at_stage(invoice, now=ist(2026, 9, 14, 10)),
                         Decimal('1.00'))


class AlertMuteTests(TrackerFlowTestCase):
    """"Stop emailing me about this one" — scoped to the stage VISIT."""

    def _at_pre_audit(self):
        return self.move_to(self.make_invoice(), 'pre_audit')

    def test_muting_records_the_reason_and_the_user(self):
        invoice = self._at_pre_audit()
        mute = set_alert_mute(invoice, self.clerk, 'vendor is sending a credit note')
        self.assertTrue(mute.is_active)
        self.assertEqual(mute.reason, 'vendor is sending a credit note')
        self.assertEqual(mute.created_by, self.clerk)
        self.assertEqual(mute.stage, invoice.current_stage)
        self.assertEqual(mute.stage_entered_at, invoice.current_stage_entered_at)

    def test_a_reason_is_mandatory(self):
        invoice = self._at_pre_audit()
        with self.assertRaises(ValidationError):
            set_alert_mute(invoice, self.clerk, '   ')
        self.assertEqual(AlertMute.objects.count(), 0)

    def test_a_user_without_the_stage_cannot_mute(self):
        invoice = self._at_pre_audit()
        with self.assertRaises(PermissionDenied):
            set_alert_mute(invoice, self.other, 'not my desk')

    def test_the_map_finds_a_muted_invoice(self):
        invoice = self._at_pre_audit()
        set_alert_mute(invoice, self.clerk, 'awaiting paperwork')
        self.assertEqual(set(alert_mute_map([invoice.id])), {invoice.id})

    def test_the_mute_lapses_when_the_invoice_moves_on(self):
        """The point of keying on the visit: the next desk is not silenced by a
        decision the previous desk made."""
        invoice = self._at_pre_audit()
        set_alert_mute(invoice, self.clerk, 'awaiting paperwork')
        invoice = apply_action(invoice=invoice, user=self.clerk,
                               stage_status='OK', remarks='done')
        self.assertEqual(alert_mute_map([invoice.id]), {})

    def test_re_muting_the_same_visit_updates_rather_than_duplicates(self):
        invoice = self._at_pre_audit()
        set_alert_mute(invoice, self.clerk, 'first reason')
        clear_alert_mute(invoice, self.clerk)
        set_alert_mute(invoice, self.clerk, 'second reason')
        self.assertEqual(AlertMute.objects.count(), 1)
        mute = AlertMute.objects.get()
        self.assertTrue(mute.is_active)
        self.assertEqual(mute.reason, 'second reason')
        self.assertIsNone(mute.cleared_at)

    def test_clearing_keeps_the_row_as_an_audit_trail(self):
        invoice = self._at_pre_audit()
        set_alert_mute(invoice, self.clerk, 'awaiting paperwork')
        self.assertTrue(clear_alert_mute(invoice, self.clerk))
        mute = AlertMute.objects.get()
        self.assertFalse(mute.is_active)
        self.assertEqual(mute.cleared_by, self.clerk)
        self.assertIsNotNone(mute.cleared_at)
        self.assertEqual(mute.reason, 'awaiting paperwork')
        self.assertEqual(alert_mute_map([invoice.id]), {})

    def test_clearing_an_unmuted_invoice_is_a_no_op(self):
        invoice = self._at_pre_audit()
        self.assertFalse(clear_alert_mute(invoice, self.clerk))

    def test_empty_input_costs_no_query(self):
        with self.assertNumQueries(0):
            self.assertEqual(alert_mute_map([]), {})


class AlertMuteEndpointTests(TrackerFlowTestCase):
    """The HTTP round trip, because the wiring is where this can break quietly.

    Three things are only testable here and not in the service tests above: the
    route exists, `_scoped_queryset` lets the caller reach their own invoice,
    and the un-mute arrives as a DELETE carrying a JSON body (the client sends
    it as `api.delete(url, { data })`, which is easy to get wrong on either
    side). The mute RULES are covered by `AlertMuteTests`.
    """

    def setUp(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from users.models import UserRole
        from .views import AlertMuteView
        # The flow fixture's users have no role; the API layer needs one,
        # because `IsTrackerUser` gates on the tracker sub-roles.
        role, _ = UserRole.objects.get_or_create(
            name='tracker_user', defaults={'display_name': 'Tracker User'})
        self.clerk.role = role
        self.clerk.save(update_fields=['role'])
        self.factory = APIRequestFactory()
        self.force_authenticate = force_authenticate
        self.view = AlertMuteView.as_view()
        self.invoice = self.move_to(self.make_invoice(), 'pre_audit')

    def _call(self, method, payload):
        request = getattr(self.factory, method)(
            '/api/tracker/alerts/mute/', payload, format='json')
        self.force_authenticate(request, user=self.clerk)
        return self.view(request)

    def test_post_mutes_and_delete_un_mutes(self):
        response = self._call('post', {'ids': [self.invoice.id],
                                       'reason': 'awaiting credit note'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['processed'], [self.invoice.id])
        self.assertEqual(set(alert_mute_map([self.invoice.id])), {self.invoice.id})

        response = self._call('delete', {'ids': [self.invoice.id]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(alert_mute_map([self.invoice.id]), {})

    def test_a_blank_reason_is_refused_before_anything_is_written(self):
        response = self._call('post', {'ids': [self.invoice.id], 'reason': '  '})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AlertMute.objects.count(), 0)

    def test_no_ids_is_refused(self):
        response = self._call('post', {'ids': [], 'reason': 'anything'})
        self.assertEqual(response.status_code, 400)

    def test_the_queue_reports_the_flag_and_the_reason(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from .views import MyQueueView
        set_alert_mute(self.invoice, self.clerk, 'awaiting credit note')
        request = APIRequestFactory().get('/api/tracker/my-queue/')
        force_authenticate(request, user=self.clerk)
        response = MyQueueView.as_view()(request)
        self.assertEqual(response.status_code, 200)
        row = next(r for r in response.data['invoices'] if r['id'] == self.invoice.id)
        self.assertTrue(row['email_muted'])
        self.assertEqual(row['email_mute_reason'], 'awaiting credit note')
        self.assertEqual(row['email_muted_by'], self.clerk.username)


class AutoAdvanceToPaymentTests(TrackerFlowTestCase):
    """SAP already has the document, so the tracker row stops pretending it is
    still mid-flow."""

    REASON = 'Automatic progression - already saved in SAP as AP Invoice 900.'

    def test_it_lands_at_payment_from_wherever_it_was(self):
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        invoice = auto_advance_to_payment(invoice, self.clerk, self.REASON)
        self.assertEqual(invoice.current_stage.code, 'payment')

    def test_every_bypassed_desk_on_its_route_gets_a_skipped_visit(self):
        """The desks have to appear, or `reports.py` counts a population that
        silently excludes fast-forwarded invoices."""
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        route = [s.code for s in stage_route(invoice)]
        expected = [c for c in route
                    if route.index(c) > route.index('pre_audit') and c != 'payment']

        auto_advance_to_payment(invoice, self.clerk, self.REASON)

        skipped = set(StageEvent.objects
                      .filter(invoice=invoice, stage_status=SKIPPED_STATUS)
                      .exclude(stage__code='pre_audit')
                      .values_list('stage__code', flat=True))
        self.assertEqual(skipped, set(expected))

    def test_the_skipped_visits_are_zero_length_and_carry_the_reason(self):
        invoice = self.move_to(self.make_invoice(), 'data_entry')
        auto_advance_to_payment(invoice, self.clerk, self.REASON)
        # Excluding the desk it was sitting at: that one closes with its real
        # dwell time, which is also 0.00 when the test runs inside a minute.
        bypassed = (StageEvent.objects
                    .filter(invoice=invoice, stage_status=SKIPPED_STATUS)
                    .exclude(stage__code='data_entry'))
        self.assertTrue(bypassed.exists())
        for ev in bypassed:
            self.assertEqual(ev.entered_at, ev.exited_at)
            self.assertEqual(ev.remarks, self.REASON)

    def test_the_desk_it_was_actually_sitting_at_keeps_its_real_dwell_time(self):
        """Only the untouched desks ahead of it are zero-length; the one that
        held the invoice must not have its ageing erased."""
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        StageEvent.objects.filter(invoice=invoice, stage__code='pre_audit',
                                  exited_at__isnull=True).update(
            entered_at=timezone.now() - timedelta(days=3))

        auto_advance_to_payment(invoice, self.clerk, self.REASON)

        closed = StageEvent.objects.get(invoice=invoice, stage__code='pre_audit',
                                        exited_at__isnull=False)
        self.assertGreater(closed.days_spent, Decimal('0.00'))

    def test_it_opens_a_receive_at_payment(self):
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        invoice = auto_advance_to_payment(invoice, self.clerk, self.REASON)
        visit = _open_event(invoice)
        self.assertIsNotNone(visit)
        self.assertEqual(visit.stage.code, 'payment')
        self.assertEqual(visit.event_type, StageEvent.EventType.RECEIVE)

    def test_it_works_from_a_detour_desk(self):
        """Transport Approval is deliberately NOT on `stage_route`, so a
        list-index lookup would raise for exactly these invoices."""
        invoice = self.move_to(self.make_invoice(self.transport), 'pre_audit')
        invoice = apply_action(invoice=invoice, user=self.clerk,
                               stage_status='OK', remarks='detour')
        self.assertEqual(invoice.current_stage.code, 'transport_approval')
        self.grant(self.clerk, 'transport_approval')
        invoice = auto_advance_to_payment(invoice, self.clerk, self.REASON)
        self.assertEqual(invoice.current_stage.code, 'payment')

    def test_a_reason_is_mandatory(self):
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        with self.assertRaises(ValidationError):
            auto_advance_to_payment(invoice, self.clerk, '   ')

    def test_an_invoice_already_at_payment_is_refused(self):
        invoice = self.move_to(self.make_invoice(), 'payment')
        with self.assertRaises(ValidationError):
            auto_advance_to_payment(invoice, self.clerk, self.REASON)

    def test_a_pending_rejection_is_cleared_on_the_way(self):
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        Invoice.objects.filter(pk=invoice.pk).update(rejection_pending=True)
        invoice.refresh_from_db()
        invoice = auto_advance_to_payment(invoice, self.clerk, self.REASON)
        self.assertFalse(invoice.rejection_pending)

    def test_the_system_may_act_without_a_user(self):
        """The nightly sweep has no person to attribute the move to."""
        invoice = self.move_to(self.make_invoice(), 'pre_audit')
        invoice = auto_advance_to_payment(invoice, None, self.REASON)
        self.assertEqual(invoice.current_stage.code, 'payment')
        self.assertIsNone(
            StageEvent.objects.filter(invoice=invoice, stage__code='payment')
            .first().acted_by)


class SapSavedRemarksTests(TestCase):
    """The sentence has to name the document, or the jump is unverifiable."""

    def test_it_names_the_document_it_matched(self):
        text = sap_saved_remarks({
            'doc_type': 'AP_INVOICE', 'docnum': 12345, 'docentry': 678,
            'table': 'OPCH', 'schema': 'JIVO_OIL_HANADB',
            'doc_date': date(2026, 9, 12),
        })
        for fragment in ('Automatic progression', 'Ap Invoice', '12345',
                         'OPCH', '678', 'JIVO_OIL_HANADB', '12-09-2026'):
            self.assertIn(fragment, text)

    def test_a_missing_date_does_not_break_it(self):
        text = sap_saved_remarks({'doc_type': 'AP_CREDIT_MEMO', 'docnum': 1,
                                  'docentry': 2, 'table': 'ORPC',
                                  'schema': 'X', 'doc_date': None})
        self.assertIn('Automatic progression', text)
        self.assertNotIn('dated', text)


class SyncSapSavedTests(TrackerFlowTestCase):
    """The sweep itself, with SAP stubbed — the HANA query is not what is
    under test here, the selection and the idempotence are."""

    def _vendor_invoice(self, number, stage):
        """A tracker invoice with an SAP vendor code — without one it is
        correctly never a candidate, since the match key needs it."""
        invoice = self.move_to(self.make_invoice(number=number), stage)
        Invoice.objects.filter(pk=invoice.pk).update(party_code='VENDA000001')
        invoice.refresh_from_db()
        return invoice

    def _stub(self, mapping):
        """Patch the batched lookup to return `mapping` (invoice id -> doc)."""
        from unittest.mock import patch
        doc = {'table': 'OPCH', 'docnum': 900, 'docentry': 11,
               'doc_type': 'AP_INVOICE', 'schema': 'S', 'doc_date': None}
        return patch.multiple(
            'tracker.sap',
            posted_documents_for=lambda invoices: {
                i.id: doc for i in invoices if i.id in mapping},
            cross_company_documents_for=lambda invoices: {},
        )

    def test_only_invoices_sap_has_are_moved(self):
        moved = self._vendor_invoice('S-1', 'pre_audit')
        left = self._vendor_invoice('S-2', 'pre_audit')
        with self._stub({moved.id}):
            result = sync_sap_saved()
        self.assertEqual([r['invoice'].id for r in result['advanced']], [moved.id])
        moved.refresh_from_db()
        left.refresh_from_db()
        self.assertEqual(moved.current_stage.code, 'payment')
        self.assertEqual(left.current_stage.code, 'pre_audit')

    def test_the_from_stage_is_captured_before_the_move(self):
        invoice = self._vendor_invoice('S-3', 'data_entry')
        with self._stub({invoice.id}):
            result = sync_sap_saved()
        self.assertEqual(result['advanced'][0]['from_stage'], 'Data Entry')

    def test_dry_run_changes_nothing(self):
        invoice = self._vendor_invoice('S-4', 'pre_audit')
        with self._stub({invoice.id}):
            result = sync_sap_saved(dry_run=True)
        self.assertEqual(len(result['advanced']), 1)
        invoice.refresh_from_db()
        self.assertEqual(invoice.current_stage.code, 'pre_audit')

    def test_running_it_twice_is_a_no_op_the_second_time(self):
        invoice = self._vendor_invoice('S-5', 'pre_audit')
        with self._stub({invoice.id}):
            sync_sap_saved()
            again = sync_sap_saved()
        self.assertEqual(again['advanced'], [])

    def test_an_invoice_without_a_vendor_code_is_never_a_candidate(self):
        """The match key is NumAtCard + CardCode; without the code a match
        would be a guess across every vendor that reused the number."""
        invoice = self._vendor_invoice('S-6', 'pre_audit')
        Invoice.objects.filter(pk=invoice.pk).update(party_code='')
        with self._stub({invoice.id}):
            result = sync_sap_saved()
        self.assertEqual(result['checked'], 0)


class SapSavedSyncEndpointTests(TrackerFlowTestCase):
    """The button's endpoint: who may press it, and that it calls the same
    service the nightly job does."""

    def setUp(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from users.models import UserRole
        from .views import SapSavedSyncView
        role, _ = UserRole.objects.get_or_create(
            name='tracker_user', defaults={'display_name': 'Tracker User'})
        for user in (self.clerk, self.other):
            user.role = role
            user.save(update_fields=['role'])
        self.factory = APIRequestFactory()
        self.force_authenticate = force_authenticate
        self.view = SapSavedSyncView.as_view()

    def _post(self, user, payload=None):
        request = self.factory.post('/api/tracker/actions/sync-sap-saved/',
                                    payload or {}, format='json')
        self.force_authenticate(request, user=user)
        return self.view(request)

    def test_a_user_without_the_save_in_sap_desk_is_refused(self):
        """It moves other desks' invoices to Payment, so it is not open to
        every tracker user."""
        self.grant(self.other, 'pre_audit')
        self.assertEqual(self._post(self.other).status_code, 403)

    def test_the_save_in_sap_desk_may_run_it(self):
        self.grant(self.clerk, 'save_in_sap')
        response = self._post(self.clerk)
        self.assertEqual(response.status_code, 200)
        self.assertIn('advanced_count', response.data)

    def test_it_advances_what_sap_has_and_reports_the_document(self):
        from unittest.mock import patch
        self.grant(self.clerk, 'save_in_sap')
        invoice = self.move_to(self.make_invoice(number='EP-1'), 'pre_audit')
        Invoice.objects.filter(pk=invoice.pk).update(party_code='VENDA000001')
        doc = {'table': 'OPCH', 'docnum': 900, 'docentry': 11,
               'doc_type': 'AP_INVOICE', 'schema': 'S', 'doc_date': None}
        with patch.multiple(
            'tracker.sap',
            posted_documents_for=lambda invoices: {i.id: doc for i in invoices},
            cross_company_documents_for=lambda invoices: {},
        ):
            response = self._post(self.clerk)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['advanced_count'], 1)
        row = response.data['advanced'][0]
        self.assertEqual(row['invoice_number'], 'EP-1')
        self.assertEqual(row['from_stage'], 'Pre-Audit')
        self.assertEqual(row['sap_docnum'], 900)
        invoice.refresh_from_db()
        self.assertEqual(invoice.current_stage.code, 'payment')


class SapUnavailableTests(TrackerFlowTestCase):
    """A failed SAP lookup must not read as "nothing found".

    This is the bug that made the JSAP refresh button report
    "N still awaiting a JSAP decision" through a HANA outage: the invoices were
    not awaiting anything, the lookup had failed and said nothing about it.
    """

    def setUp(self):
        self.invoice = self.move_to(self.make_invoice(number='U-1'), 'jsap_approval')
        Invoice.objects.filter(pk=self.invoice.pk).update(party_code='VENDA000001')
        self.invoice.refresh_from_db()

    @staticmethod
    def _hana_down():
        from unittest.mock import patch
        return patch('tracker.sap.HANAConnection',
                     side_effect=OSError('connection refused'))

    def test_a_lenient_lookup_still_swallows_the_failure(self):
        """Most callers want best-effort, and that behaviour is unchanged."""
        from tracker import sap
        with self._hana_down():
            self.assertEqual(sap.find_draft_documents('X', 'VENDA000001', 'S'), [])

    def test_a_strict_lookup_raises_instead(self):
        from tracker import sap
        with self._hana_down():
            with self.assertRaises(sap.SapUnavailable):
                sap.find_draft_documents('X', 'VENDA000001', 'S', strict=True)

    def test_the_jsap_status_says_unreachable_not_no_draft(self):
        from tracker import jsap
        with self._hana_down():
            status = jsap.status_for_invoice(self.invoice)
        self.assertFalse(status['available'])
        self.assertEqual(status['reason'], 'sap_unreachable')

    def test_the_sweep_counts_it_apart_from_waiting(self):
        """`waiting` means pending; an unreachable SAP is not pending."""
        from tracker import services
        with self._hana_down():
            result = services.sync_jsap_all()
        self.assertIn(self.invoice.pk, result['unreachable'])
        self.assertNotIn(self.invoice.pk, result['waiting'])
        self.assertEqual(result['advanced'], [])

    def test_the_invoice_is_not_moved_when_sap_cannot_be_reached(self):
        from tracker import services
        with self._hana_down():
            services.sync_jsap_all()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.current_stage.code, 'jsap_approval')


class DraftReCreationTests(TrackerFlowTestCase):
    """JSAP's approval stays on whichever draft was current when it was granted.

    A draft deleted and re-made keeps the same number and vendor but gets a new
    DocEntry, so taking only the newest made the desk wait for a decision that
    had already been made. Measured against production: 1 of 172.
    """

    OLD = {'docentry': 100, 'docnum': 1, 'canceled': 'N', 'schema': 'S',
           'table': 'ODRF', 'object_type': 18, 'num_at_card': 'D-1',
           'card_code': 'VENDA000001', 'card_name': 'V', 'doc_date': None,
           'doc_total': 0}
    NEW = {**OLD, 'docentry': 200}

    def setUp(self):
        self.invoice = self.move_to(self.make_invoice(number='D-1'), 'jsap_approval')
        Invoice.objects.filter(pk=self.invoice.pk).update(party_code='VENDA000001')
        self.invoice.refresh_from_db()

    def _drafts(self, hits):
        from unittest.mock import patch
        return patch('tracker.sap.find_draft_documents', return_value=list(hits))

    def test_every_draft_is_returned_newest_first(self):
        from tracker import sap
        with self._drafts([self.OLD, self.NEW]):
            got = sap.resolve_draft_documents(self.invoice)
        self.assertEqual([d['docentry'] for d in got], [200, 100])

    def test_a_cancelled_draft_sorts_last(self):
        from tracker import sap
        cancelled = {**self.NEW, 'canceled': 'Y'}
        with self._drafts([cancelled, self.OLD]):
            got = sap.resolve_draft_documents(self.invoice)
        self.assertEqual([d['docentry'] for d in got], [100, 200])

    def test_the_approval_on_an_older_draft_is_still_found(self):
        """The whole point: JSAP knows 100, not the re-made 200."""
        from unittest.mock import patch
        from tracker import jsap

        def only_the_old_one(docentry, branch=None):
            if docentry != 100:
                return None
            return {'status': 'A', 'label': 'Approved', 'doc_id': 1,
                    'doc_entry': 100, 'branch': branch, 'updated_on': None,
                    'created_on': None, 'description': 'ok', 'decided_on': None,
                    'decided_by': None}

        with self._drafts([self.OLD, self.NEW]), \
                patch.object(jsap, 'status_for_draft', only_the_old_one):
            status = jsap.status_for_invoice(self.invoice)

        self.assertTrue(status['available'])
        self.assertEqual(status['status'], 'A')
        self.assertEqual(status['draft']['docentry'], 100)

    def test_no_draft_known_to_jsap_still_reports_not_submitted(self):
        from unittest.mock import patch
        from tracker import jsap
        with self._drafts([self.OLD, self.NEW]), \
                patch.object(jsap, 'status_for_draft', lambda *a, **k: None):
            status = jsap.status_for_invoice(self.invoice)
        self.assertFalse(status['available'])
        self.assertEqual(status['reason'], 'not_submitted')
