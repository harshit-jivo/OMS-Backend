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
from datetime import date
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone

from users.models import User

from .management.commands.seed_tracker import STAGES
from .models import (
    Branch, Category, GstRate, GstType, Invoice, InvoiceMode, PaymentDetail,
    Stage, StageEvent, Unit, UserStageAccess,
)
from .services import (
    SKIPPED_STATUS, _open_event, apply_action, create_invoice, fast_track,
    full_hold_invoice_ids, stage_route,
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
