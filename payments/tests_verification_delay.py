"""How long collected money waits for its handover check.

The business question: a collector takes cash on Monday and it only reaches a
verifier on Friday — the company was carrying that risk for four days. The
figure is measured from `payment_date` (when the money changed hands) to
`verified_at`, NOT from `created_at`: cash collected Monday and typed up
Thursday sat in a pocket for three days, and an entry-to-verification figure
would score that as same-day.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from users.models import User, UserRole

from .analytics import VERIFICATION_SLA_DAYS, collection_performance
from .models import PaymentReceipt


def _receipt(no, creator, *, collected_on, verified_on=None, amount='100.00'):
    receipt = PaymentReceipt.objects.create(
        receipt_no=no, company='OIL', card_code='CUST1',
        payment_date=collected_on, total_amount=Decimal(amount),
        is_advance=True, sap_branch_id=1, created_by=creator,
        status=PaymentReceipt.Status.POSTED,
        verification_status=(
            PaymentReceipt.VerificationStatus.VERIFIED if verified_on
            else PaymentReceipt.VerificationStatus.PENDING),
    )
    if verified_on:
        # Midday, so a timezone conversion cannot roll the date either way.
        receipt.verified_at = timezone.make_aware(
            timezone.datetime.combine(verified_on, timezone.datetime.min.time())
            + timedelta(hours=12))
        receipt.save(update_fields=['verified_at'])
    return receipt


class VerificationDelayTests(TestCase):
    def setUp(self):
        role, _ = UserRole.objects.get_or_create(name='payments_and_deposit')
        self.collector = User.objects.create(
            username='vd_collector', name='Collector', role=role)
        self.start = date(2026, 1, 1)
        self.end = date(2026, 12, 31)

    def _row(self, username='vd_collector'):
        # `participants='user'`: these cases are about the OPERATOR's entries,
        # and the table now defaults to the collection people it is really
        # about. The delay figure is computed for both identities.
        out = collection_performance(
            None, self.start, self.end, participants='user')
        return next(
            (r for r in out['results'] if r['code'] == username), None)

    def test_delay_is_measured_from_collection_not_entry(self):
        """Collected on the 1st, verified on the 6th, is five days."""
        _receipt('RC-VD-1', self.collector,
                 collected_on=date(2026, 3, 1), verified_on=date(2026, 3, 6))
        self.assertEqual(self._row()['avg_verify_days'], 5.0)

    def test_same_day_handover_is_zero(self):
        _receipt('RC-VD-2', self.collector,
                 collected_on=date(2026, 3, 1), verified_on=date(2026, 3, 1))
        self.assertEqual(self._row()['avg_verify_days'], 0.0)

    def test_average_across_several(self):
        _receipt('RC-VD-3', self.collector,
                 collected_on=date(2026, 3, 1), verified_on=date(2026, 3, 3))
        _receipt('RC-VD-4', self.collector,
                 collected_on=date(2026, 3, 1), verified_on=date(2026, 3, 5))
        row = self._row()
        self.assertEqual(row['avg_verify_days'], 3.0)   # (2 + 4) / 2
        self.assertEqual(row['worst_verify_days'], 4)

    def test_late_counts_only_beyond_the_sla(self):
        """Two days is on time; three is late."""
        _receipt('RC-VD-5', self.collector, collected_on=date(2026, 3, 1),
                 verified_on=date(2026, 3, 1) + timedelta(days=VERIFICATION_SLA_DAYS))
        _receipt('RC-VD-6', self.collector, collected_on=date(2026, 3, 1),
                 verified_on=date(2026, 3, 1) + timedelta(days=VERIFICATION_SLA_DAYS + 1))
        self.assertEqual(self._row()['late_verify_count'], 1)

    def test_unverified_is_measured_against_today(self):
        """Money nobody has touched is the case this exists to surface."""
        collected = timezone.localdate() - timedelta(days=10)
        _receipt('RC-VD-7', self.collector, collected_on=collected)
        row = self._row()
        self.assertEqual(row['avg_verify_days'], 10.0)
        self.assertEqual(row['pending_verify_count'], 1)
        self.assertEqual(row['late_verify_count'], 1)

    def test_cancelled_receipts_are_excluded(self):
        """Cancelled money was never handed over, so it is not a delay."""
        r = _receipt('RC-VD-8', self.collector, collected_on=date(2026, 3, 1))
        r.status = PaymentReceipt.Status.CANCELLED
        r.save(update_fields=['status'])
        self.assertIsNone(self._row())

    def test_the_collection_person_carries_the_delay(self):
        """The figure belongs to whoever HELD the money, not who typed it up.

        This is the question an admin asks of the table: how long did Goldy
        hold this cash before handing it over.
        """
        from .models import CollectionPerson

        person = CollectionPerson.objects.create(
            name='Handover Person', code='HP1', company='OIL')
        receipt = _receipt('RC-VD-9', self.collector,
                           collected_on=date(2026, 3, 1),
                           verified_on=date(2026, 3, 6))
        receipt.received_from_person = person
        receipt.received_from_type = PaymentReceipt.ReceivedFromType.PERSON
        receipt.save(update_fields=['received_from_person',
                                    'received_from_type'])

        out = collection_performance(None, self.start, self.end)
        # By CODE, not "the first person row": the shared TEST database holds
        # real collection people whose receipts fall in the same window.
        person_row = next(r for r in out['results']
                          if r['kind'] == 'person' and r['code'] == person.code)
        self.assertEqual(person_row['avg_verify_days'], 5.0)

    def test_the_table_lists_collection_people_not_logins(self):
        """An admin reads this to analyse collectors, not operator activity.

        Listing both also DOUBLE-COUNTED: a receipt raised by an operator from
        a collection person appeared in full on each row, so the column summed
        to roughly twice the money that exists.
        """
        from .models import CollectionPerson

        person = CollectionPerson.objects.create(
            name='Only Person', code='OP1', company='OIL')
        receipt = _receipt('RC-VD-11', self.collector,
                           collected_on=date(2026, 3, 1),
                           verified_on=date(2026, 3, 2))
        receipt.received_from_person = person
        receipt.received_from_type = PaymentReceipt.ReceivedFromType.PERSON
        receipt.save(update_fields=['received_from_person',
                                    'received_from_type'])

        out = collection_performance(None, self.start, self.end)
        kinds = {r['kind'] for r in out['results']}
        self.assertEqual(kinds, {'person'})
        # ...and the login is still reachable when explicitly asked for.
        as_users = collection_performance(
            None, self.start, self.end, participants='user')
        self.assertEqual({r['kind'] for r in as_users['results']}, {'user'})

    def test_sorting_by_delay_does_not_crash_on_nulls(self):
        """A row with no receipts has no delay, and None cannot compare.

        A person who only BANKED a deposit — never received a payment — has no
        collection-to-verification span at all, so their figure is null and
        must not raise when the table is sorted slowest-first.
        """
        from .models import CollectionPerson

        slow = CollectionPerson.objects.create(
            name='Slow', code='SLOW1', company='OIL')
        receipt = _receipt('RC-VD-10', self.collector,
                           collected_on=date(2026, 3, 1),
                           verified_on=date(2026, 3, 9))
        receipt.received_from_person = slow
        receipt.received_from_type = PaymentReceipt.ReceivedFromType.PERSON
        receipt.save(update_fields=['received_from_person',
                                    'received_from_type'])

        # A banker with no collections of their own.
        from .models import BankDeposit

        banker = CollectionPerson.objects.create(
            name='Banker', code='BANK1', company='OIL')
        BankDeposit.objects.create(
            deposit_no='DEP-VD-1', company='OIL', deposit_date=date(2026, 3, 4),
            collected_amount=Decimal('50.00'), deposit_amount=Decimal('50.00'),
            deposited_by=banker, status=PaymentReceipt.Status.POSTED)

        out = collection_performance(
            None, self.start, self.end, sort='avg_verify_days',
            direction='desc')
        rows = out['results']

        # The claim is the ORDERING, and it has to hold across every row the
        # table returns — on the shared TEST database that includes real
        # collectors, not just the two created here. Asserting the property
        # rather than `results[0]` is also the stronger statement.
        figures = [r['avg_verify_days'] for r in rows]
        present = [f for f in figures if f is not None]
        self.assertEqual(present, sorted(present, reverse=True),
                         'slowest first')
        self.assertEqual(figures, present + [None] * (len(figures) - len(present)),
                         'rows with no figure sort to the bottom, never between')

        # ...and this test's own two rows obey it: the slow collector carries
        # the 8-day figure and sorts above the banker, who has none at all.
        def row_for(person):
            return next(r for r in rows
                        if r['kind'] == 'person' and r['code'] == person.code)

        self.assertEqual(row_for(slow)['avg_verify_days'], 8.0)
        self.assertIsNone(row_for(banker)['avg_verify_days'])
        self.assertLess(rows.index(row_for(slow)), rows.index(row_for(banker)))

    def test_the_sla_is_published_to_the_client(self):
        """So the UI colours "late" with the same number the server counted."""
        out = collection_performance(None, self.start, self.end)
        self.assertEqual(out['verification_sla_days'], VERIFICATION_SLA_DAYS)
