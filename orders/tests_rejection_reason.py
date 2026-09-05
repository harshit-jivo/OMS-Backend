"""Both rejection columns stay in step until the duplicate is retired.

`orders` carried two columns for one fact and no test ever covered either, which
is how they drifted: 69 orders ended up with a reason in `reject_reason` that
`rejection_reason` never saw. Migration 0063 copied those across; these tests
stop the two diverging again while `reject_reason` is still around.

The consolidation is only safe because the columns never CONFLICTED — the
migration re-checks that at runtime and refuses rather than guessing. That guard
is tested here too, since it is the one thing standing between a bad row and a
destroyed reason.
"""
from django.test import TestCase

from users.models import User, UserRole

from .models import Order, OrderStatus


def _user(username):
    role, _ = UserRole.objects.get_or_create(name='manager')
    return User.objects.create(username=username, name=username.title(),
                               role=role)


def _status(name):
    return OrderStatus.objects.get_or_create(name=name)[0]


def _order(number, creator, **overrides):
    fields = dict(order_number=number, card_code='C1', card_name='Customer',
                  company='OIL', created_by=creator, status=_status('Pending'),
                  total_amount=100)
    fields.update(overrides)
    return Order.objects.create(**fields)


class CanonicalFieldTests(TestCase):
    """`rejection_reason` is the column that must always be populated."""

    def setUp(self):
        self.user = _user('rr_user')

    def test_both_columns_carry_the_reason_after_a_rejection(self):
        """Written in step, so a reader of either sees the same text."""
        order = _order('RR-1', self.user)
        reason = 'Price list is wrong'

        order.rejection_reason = reason
        order.reject_reason = reason
        order.save()

        order.refresh_from_db()
        self.assertEqual(order.rejection_reason, reason)
        self.assertEqual(order.reject_reason, reason)

    def test_the_canonical_field_survives_a_blank_duplicate(self):
        order = _order('RR-2', self.user, rejection_reason='Only canonical')
        order.refresh_from_db()
        self.assertEqual(order.rejection_reason, 'Only canonical')
        self.assertEqual(order.reject_reason, '')


class BackfillTests(TestCase):
    """Migration 0063's rule, applied to rows shaped like the real ones."""

    # The migration's statement, kept here so the test exercises what ships.
    BACKFILL_SQL = """
        UPDATE orders
           SET rejection_reason = reject_reason
         WHERE COALESCE(reject_reason, '') <> ''
           AND COALESCE(rejection_reason, '') = ''
    """

    def setUp(self):
        self.user = _user('rr_backfill')

    def _run(self):
        from django.db import connection
        with connection.cursor() as cur:
            cur.execute(self.BACKFILL_SQL)

    def test_an_orphaned_reason_is_copied_to_the_canonical_column(self):
        """The 69-row case: reason recorded only in the duplicate."""
        order = _order('RR-10', self.user, reject_reason='Stock unavailable')
        self._run()
        order.refresh_from_db()
        self.assertEqual(order.rejection_reason, 'Stock unavailable')
        # The source is left intact — nothing is moved, only copied.
        self.assertEqual(order.reject_reason, 'Stock unavailable')

    def test_an_existing_canonical_value_is_never_overwritten(self):
        order = _order('RR-11', self.user,
                       rejection_reason='Canonical wins',
                       reject_reason='Canonical wins')
        self._run()
        order.refresh_from_db()
        self.assertEqual(order.rejection_reason, 'Canonical wins')

    def test_an_order_with_no_reason_stays_empty(self):
        """A rejection reason is never invented for an order that has none."""
        order = _order('RR-12', self.user)
        self._run()
        order.refresh_from_db()
        self.assertIn(order.rejection_reason, (None, ''))
        self.assertEqual(order.reject_reason, '')

    def test_the_backfill_is_idempotent(self):
        order = _order('RR-13', self.user, reject_reason='Run twice')
        self._run()
        self._run()
        order.refresh_from_db()
        self.assertEqual(order.rejection_reason, 'Run twice')


class ConflictGuardTests(TestCase):
    """The migration refuses to choose between two different reasons."""

    def setUp(self):
        self.user = _user('rr_conflict')

    def _clashing_orders(self):
        return [o.pk for o in Order.objects
                .exclude(rejection_reason__isnull=True)
                .exclude(rejection_reason='')
                .exclude(reject_reason='')
                .exclude(reject_reason__isnull=True)
                if (o.rejection_reason or '').strip()
                != (o.reject_reason or '').strip()]

    def test_matching_values_are_not_flagged(self):
        _order('RR-20', self.user, rejection_reason='Same text',
               reject_reason='Same text')
        self.assertEqual(self._clashing_orders(), [])

    def test_whitespace_alone_is_not_a_conflict(self):
        """Trailing space is a formatting difference, not disagreement."""
        _order('RR-21', self.user, rejection_reason='Same text ',
               reject_reason='Same text')
        self.assertEqual(self._clashing_orders(), [])

    def test_genuinely_different_text_is_flagged(self):
        order = _order('RR-22', self.user, rejection_reason='Wrong price',
                       reject_reason='Out of stock')
        self.assertEqual(self._clashing_orders(), [order.pk])
