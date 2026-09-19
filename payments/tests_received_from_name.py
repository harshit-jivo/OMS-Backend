"""The received-from name is frozen onto the receipt, not looked up live.

A receipt is a record of something that happened. Renaming a collector in the
master changes who they are TODAY; it must not change what was written on a
document months ago. These tests pin that the snapshot is taken when the person
is chosen, survives a later rename, and moves only when the person themselves
is changed on the receipt.
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from users.models import User, UserRole

from .tests_support import uniq
from .models import (
    CollectionPerson,
    PaymentMethodEntry,
    PaymentReceipt,
)
from .permissions import PAYMENTS_CREATE
from .serializers import PaymentReceiptCreateSerializer, PaymentReceiptSerializer


def _user(username, keys=()):
    role, _ = UserRole.objects.get_or_create(name='payments_and_deposit')
    return User.objects.create(
        username=username, name=username.title(), role=role,
        extra_pages=list(keys))


def _company():
    """No-op: the SAP company database comes from settings, not a table.

    Kept as a call so the fixtures still read as "this test has a configured
    company" rather than silently dropping the concept.
    """


def _person(name, code):
    # `code` is unique and the shared TEST database already holds real
    # collection people — GOLDY among them. Every assertion here is on the
    # NAME (that is the whole subject of this file), so the code is made
    # unique once here rather than at each of the fifteen call sites.
    return CollectionPerson.objects.create(
        name=name, code=uniq(f'{code[:12]}-'), company='OIL')


# The backfill migration's rule, kept here verbatim so the test proves the
# statement that actually ships. Written in portable SQL: the migration runs on
# PostgreSQL, this suite on SQLite, and UPDATE..FROM is not shared between them.
BACKFILL_SQL = """
    UPDATE payment_receipt
       SET received_from_name = (
           SELECT p.name FROM payment_collection_person p
            WHERE p.id = payment_receipt.received_from_person_id)
     WHERE received_from_person_id IS NOT NULL
       AND received_from_name = ''
"""


class _WriteMixin:
    def _save(self, payload, instance=None):
        request = type('R', (), {'user': self.user})()
        serializer = PaymentReceiptCreateSerializer(
            instance, data=payload, partial=instance is not None,
            context={'request': request})
        serializer.is_valid(raise_exception=True)
        return serializer.save()

    def _create_payload(self, person, **extra):
        payload = {
            'company': 'OIL', 'card_code': 'CUST1', 'card_name': 'Customer One',
            'received_from_type': 'PERSON', 'received_from_person': person.pk,
            'payment_date': '2026-09-01', 'is_advance': True,
            'methods': [{'method': 'UPI', 'amount': '1000.00',
                         'upi_reference': 'UTR-1'}],
        }
        payload.update(extra)
        return payload


class SnapshotAtCreationTests(_WriteMixin, TestCase):
    """§6 — the name is persisted when the receipt is written."""

    def setUp(self):
        self.user = _user('rf_creator', [PAYMENTS_CREATE])
        _company()

    def test_creation_stores_the_person_name(self):
        person = _person('ARVINDER SINGH', 'ARVINDER')
        receipt = self._save(self._create_payload(person))
        receipt.refresh_from_db()
        self.assertEqual(receipt.received_from_name, 'ARVINDER SINGH')
        # The relation is untouched — it remains the analytics identity.
        self.assertEqual(receipt.received_from_person_id, person.pk)

    def test_party_receipt_has_a_blank_name_not_an_invented_one(self):
        """Money straight from a party has no collector to name."""
        payload = self._create_payload(_person('Unused', 'UNUSED'))
        payload['received_from_type'] = 'PARTY'
        payload['received_from_person'] = None
        receipt = self._save(payload)
        receipt.refresh_from_db()
        self.assertEqual(receipt.received_from_name, '')
        self.assertIsNone(receipt.received_from_person_id)

    def test_the_client_cannot_set_the_name_directly(self):
        """It is derived from the FK; a free-text name could contradict it."""
        person = _person('ARVINDER SINGH', 'ARVINDER')
        payload = self._create_payload(person)
        payload['received_from_name'] = 'SOMEONE ELSE ENTIRELY'
        receipt = self._save(payload)
        receipt.refresh_from_db()
        self.assertEqual(receipt.received_from_name, 'ARVINDER SINGH')


class SnapshotSurvivesRenameTests(_WriteMixin, TestCase):
    """§16 — the worked example from the brief, end to end."""

    def setUp(self):
        self.user = _user('rf_rename', [PAYMENTS_CREATE])
        _company()

    def test_master_rename_does_not_reach_an_existing_receipt(self):
        person = _person('ARVINDER SINGH', 'ARVINDER')
        receipt = self._save(self._create_payload(person))

        person.name = 'ARVINDER SINGH IMPREST JWPL0115'
        person.save(update_fields=['name'])

        receipt.refresh_from_db()
        self.assertEqual(receipt.received_from_name, 'ARVINDER SINGH')
        self.assertEqual(receipt.received_from_person_id, person.pk)

    def test_the_api_serves_the_frozen_name_after_a_rename(self):
        person = _person('ARVINDER SINGH', 'ARVINDER')
        receipt = self._save(self._create_payload(person))
        person.name = 'ARVINDER SINGH IMPREST JWPL0115'
        person.save(update_fields=['name'])

        receipt.refresh_from_db()
        data = PaymentReceiptSerializer(receipt).data
        self.assertEqual(data['received_from_name'], 'ARVINDER SINGH')

    def test_a_new_receipt_after_the_rename_gets_the_new_name(self):
        """The snapshot is per-receipt, not a global freeze."""
        person = _person('ARVINDER SINGH', 'ARVINDER')
        old = self._save(self._create_payload(person))
        person.name = 'ARVINDER SINGH IMPREST JWPL0115'
        person.save(update_fields=['name'])

        new = self._save(self._create_payload(person, card_code='CUST2'))
        old.refresh_from_db()
        new.refresh_from_db()
        self.assertEqual(old.received_from_name, 'ARVINDER SINGH')
        self.assertEqual(new.received_from_name,
                         'ARVINDER SINGH IMPREST JWPL0115')


class SnapshotOnEditTests(_WriteMixin, TestCase):
    """§7 — moves when the person changes, stays put otherwise."""

    def setUp(self):
        self.user = _user('rf_editor', [PAYMENTS_CREATE])
        _company()
        self.person = _person('ARVINDER SINGH', 'ARVINDER')
        self.receipt = self._save(self._create_payload(self.person))

    def test_changing_the_person_updates_the_snapshot(self):
        other = _person('GOLDY', 'GOLDY')
        self._save({'received_from_person': other.pk}, instance=self.receipt)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.received_from_name, 'GOLDY')
        self.assertEqual(self.receipt.received_from_person_id, other.pk)

    def test_an_unrelated_edit_leaves_the_snapshot_alone(self):
        """A remarks edit must not silently re-resolve the name."""
        self.person.name = 'RENAMED SINCE'
        self.person.save(update_fields=['name'])

        self._save({'remarks': 'Corrected note'}, instance=self.receipt)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.received_from_name, 'ARVINDER SINGH')

    def test_switching_to_party_clears_the_snapshot(self):
        self._save({'received_from_type': 'PARTY',
                    'received_from_person': None}, instance=self.receipt)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.received_from_name, '')


class BackfillTests(TestCase):
    """§5 — the migration's rule, applied to a row written before the column."""

    def test_blank_snapshot_is_filled_from_the_relation(self):
        person = _person('ARVINDER SINGH', 'ARVINDER')
        creator = _user('rf_backfill', [PAYMENTS_CREATE])
        # A pre-migration row: FK set, snapshot still blank.
        receipt = PaymentReceipt.objects.create(
            receipt_no='RC-BF-1', company='OIL', card_code='CUST1',
            payment_date=date(2026, 9, 1), total_amount=Decimal('100.00'),
            is_advance=True, sap_branch_id=1,
            received_from_type='PERSON', received_from_person=person,
            received_from_name='', created_by=creator)
        PaymentMethodEntry.objects.create(
            receipt=receipt, method=PaymentMethodEntry.Method.UPI,
            upi_reference='UTR-BF', amount=Decimal('100.00'))

        from django.db import connection
        with connection.cursor() as cur:
            cur.execute(BACKFILL_SQL)

        receipt.refresh_from_db()
        self.assertEqual(receipt.received_from_name, 'ARVINDER SINGH')

    def test_backfill_does_not_touch_a_receipt_with_no_person(self):
        creator = _user('rf_backfill2', [PAYMENTS_CREATE])
        receipt = PaymentReceipt.objects.create(
            receipt_no='RC-BF-2', company='OIL', card_code='CUST1',
            payment_date=date(2026, 9, 1), total_amount=Decimal('100.00'),
            is_advance=True, sap_branch_id=1,
            received_from_type='PARTY', received_from_person=None,
            received_from_name='', created_by=creator)

        from django.db import connection
        with connection.cursor() as cur:
            cur.execute(BACKFILL_SQL)

        receipt.refresh_from_db()
        self.assertEqual(receipt.received_from_name, '')


class UnchangedBehaviourTests(_WriteMixin, TestCase):
    """§10, §11 — what this change deliberately does NOT alter."""

    def setUp(self):
        self.user = _user('rf_intact', [PAYMENTS_CREATE])
        _company()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_received_from_type_still_validates(self):
        person = _person('ARVINDER SINGH', 'ARVINDER')
        payload = self._create_payload(person)
        payload['received_from_person'] = None      # PERSON without a person
        request = type('R', (), {'user': self.user})()
        serializer = PaymentReceiptCreateSerializer(
            data=payload, context={'request': request})
        self.assertFalse(serializer.is_valid())
        self.assertIn('received_from_person', serializer.errors)

    def test_the_api_still_exposes_type_and_person_id(self):
        """Mobile branches on both; neither may disappear."""
        person = _person('ARVINDER SINGH', 'ARVINDER')
        receipt = self._save(self._create_payload(person))
        data = PaymentReceiptSerializer(receipt).data
        self.assertEqual(data['received_from_type'], 'PERSON')
        self.assertEqual(data['received_from_person'], person.pk)
        self.assertEqual(data['received_from_name'], 'ARVINDER SINGH')

    def test_analytics_still_groups_by_person_id(self):
        """Two collectors sharing a name stay distinct — the point of the FK."""
        a = _person('GOLDY', 'GOLDY-A')
        b = _person('GOLDY', 'GOLDY-B')
        self._save(self._create_payload(a))
        self._save(self._create_payload(b, card_code='CUST2'))

        # Scoped to the two collectors this test created: the shared TEST
        # database holds real receipts against real people, and the claim here
        # is about THESE two, not about the table as a whole.
        mine = PaymentReceipt.objects.filter(received_from_person__in=[a, b])
        rows = mine.values('received_from_person').distinct()
        self.assertEqual(rows.count(), 2)
        # ...even though the snapshot name is identical for both.
        names = set(mine.values_list('received_from_name', flat=True))
        self.assertEqual(names, {'GOLDY'})
