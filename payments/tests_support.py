"""Helpers that let the payments tests run against a POPULATED database.

WHY THIS EXISTS
---------------
The payments suite runs against the shared TEST PostgreSQL database, which
holds real data — receipts, deposits, users, parties, master rows — because
the TEST role cannot create a throwaway database and a second permanent one is
not wanted. Every test is a `TestCase`, so nothing a test writes survives; but
what is ALREADY there is visible to every query a test makes.

That breaks two habits this suite had picked up, both of which silently assume
an empty database:

    PaymentMethodEntry.objects.get()          # "the one I just made"
    self.assertEqual(PaymentReceipt.objects.count(), 0)

and one more that is not about counting at all — a fixture claiming a unique
key (a username, a role name, a party code) that the real data already holds,
which fails in `setUp` before the test even starts.

WHAT TO USE
-----------
`uniq()` for any value behind a unique constraint, so a fixture can never
collide with real data or with residue from someone else's run.

`only(qs, **filters)` where a test used to call a bare `.get()`.

`Delta` where a test used to compare a global count against a literal.

None of these weaken what is being asserted: "exactly one row exists for THIS
receipt" is a stronger statement than "exactly one row exists anywhere", and it
is the one the test always meant.
"""
import uuid


def uniq(prefix=''):
    """A value that cannot collide with existing data or a parallel run.

    Use for usernames, role names, party codes, document numbers — anything
    under a unique constraint. Short enough to fit the narrow `max_length`
    fields in this app (a party code is 20 characters).
    """
    return f'{prefix}{uuid.uuid4().hex[:8]}'


def only(queryset, **filters):
    """The single row matching `filters` — asserting there is exactly one.

    The scoped replacement for a bare `.get()`. `.get()` on a populated
    database raises MultipleObjectsReturned because it sees every pre-existing
    row; this narrows to the test's own data first and still fails loudly if
    the test really did create two.
    """
    rows = list(queryset.filter(**filters)[:2])
    if len(rows) != 1:
        raise AssertionError(
            f'expected exactly 1 {queryset.model.__name__} matching {filters}, '
            f'found {queryset.filter(**filters).count()}')
    return rows[0]


class Delta:
    """How much a queryset grew across a block of work.

    Replaces comparing a global count to a literal, which only ever held on an
    empty database:

        with Delta(PaymentReceipt.objects.all()) as d:
            ...do the thing...
        d.assert_grew_by(1, test)

    The delta is what the test was really asserting: the operation created one
    receipt. Whether the database already held sixty is irrelevant to that.
    """

    def __init__(self, queryset):
        self.queryset = queryset
        self.before = None
        self.after = None

    def __enter__(self):
        self.before = self.queryset.count()
        return self

    def __exit__(self, *exc):
        self.after = self.queryset.count()
        return False

    @property
    def growth(self):
        return self.after - self.before

    def assert_grew_by(self, expected, test):
        test.assertEqual(
            self.growth, expected,
            f'expected {self.queryset.model.__name__} to grow by {expected}, '
            f'it grew by {self.growth}')


def history_of(document):
    """Every `PaymentStatusHistory` row belonging to ONE document.

    The history is a generic relation (content_type + object_id), so filtering
    it by `action=` alone reaches across every receipt and deposit in the
    database — fine on an empty one, meaningless on this one.
    """
    from django.contrib.contenttypes.models import ContentType

    from .models import PaymentStatusHistory

    return PaymentStatusHistory.objects.filter(
        content_type=ContentType.objects.get_for_model(type(document)),
        object_id=document.pk)


def notifications_of(document):
    """Every `Notification` whose entity is ONE document.

    `object_id` alone is not enough: a receipt and a deposit can share an id,
    and the shared TEST database holds notifications for both.
    """
    from django.contrib.contenttypes.models import ContentType

    from notifications.models import Notification

    return Notification.objects.filter(
        content_type=ContentType.objects.get_for_model(type(document)),
        object_id=document.pk)
