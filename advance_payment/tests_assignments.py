"""Bills and POs sent to an Advance Payment User to raise a request from.

No database and no SAP: querysets and SAP are stood in for.
"""
import contextlib
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from advance_payment.models import AssignmentStatus, DocumentKind
from advance_payment.services import assignments

LIVE = {501: {'doc_num': 126226523, 'open': '288746', 'status': 'OPEN', 'card_code': 'VENDA001465',
              'card_name': 'GODAMWALE TRADING', 'vendor_ref': 'GT/88', 'doc_date': '2026-09-01',
              'due_date': '2026-10-01', 'doc_total': '300000'}}
RAHUL = SimpleNamespace(pk=21, name='Rahul')


def _send(documents=None, recipient=RAHUL, live=None, **kwargs):
    qs = mock.Mock()
    qs.filter.return_value.first.return_value = recipient
    with mock.patch.object(assignments, 'recipients', return_value=qs), \
            mock.patch('advance_payment.services.sap.live_documents', return_value=LIVE if live is None else live), \
            mock.patch.object(assignments.transaction, 'atomic', return_value=contextlib.nullcontext()), \
            mock.patch.object(assignments.DocumentAssignment.objects, 'create',
                              side_effect=lambda **f: SimpleNamespace(**f)) as create:
        made = assignments.send(company=kwargs.get('company', 'OIL'), recipient_id=kwargs.get('recipient_id', 21),
                                documents=documents if documents is not None else [{'kind': 'BILL', 'sap_doc_entry': 501}],
                                note='Please pay by Friday', user=SimpleNamespace(pk=1))
    return made, create


class WhoMayReceive(SimpleTestCase):
    def test_whoever_holds_the_raise_key_by_role_or_alone(self):
        User = mock.Mock()
        bundles = mock.Mock()
        bundles.filter.return_value.values_list.return_value = [19, 20]
        with mock.patch.object(assignments, 'get_user_model', return_value=User),                 mock.patch('users.models.RolePermissions.objects', bundles):
            assignments.recipients()
        # Roles whose bundle carries the key, and only active ones.
        self.assertEqual(bundles.filter.call_args.kwargs,
                         {'role__is_active': True, 'keys__contains': ['Advance_Payment']})
        condition = str(User.objects.filter.call_args.args[0])
        self.assertIn("('extra_pages__contains', ['Advance_Payment'])", condition)
        self.assertIn("('role_id__in', [19, 20])", condition)
        self.assertIn("('extra_roles__in', [19, 20])", condition)
        self.assertEqual(User.objects.filter.call_args.kwargs, {'is_active': True})


class Sending(SimpleTestCase):
    def test_copies_the_documents_facts_from_sap(self):
        made, _create = _send()
        [one] = made
        self.assertEqual((one.kind, one.sap_doc_num, one.card_code, one.open_amount, one.assigned_to),
                         (DocumentKind.BILL, '126226523', 'VENDA001465', '288746', RAHUL))
        self.assertEqual(one.note, 'Please pay by Friday')

    def test_only_to_an_advance_payment_user(self):
        with self.assertRaisesRegex(assignments.AssignmentInvalid, 'an active user who can raise payment requests'):
            _send(recipient=None)

    def test_refuses_a_document_not_open_in_sap(self):
        closed = {501: {**LIVE[501], 'status': 'CLOSED'}}
        with self.assertRaisesRegex(assignments.AssignmentInvalid, 'is closed in SAP'):
            _send(live=closed)
        with self.assertRaisesRegex(assignments.AssignmentInvalid, 'is not in OIL'):
            _send(live={})

    def test_needs_a_kind_and_an_entry(self):
        with self.assertRaisesRegex(assignments.AssignmentInvalid, 'its kind'):
            _send(documents=[{'kind': 'GRN', 'sap_doc_entry': 1}])
        with self.assertRaisesRegex(assignments.AssignmentInvalid, 'at least one'):
            _send(documents=[])


def _assignment(**extra):
    values = dict(pk=5, assigned_to_id=21, assigned_by_id=1, status=AssignmentStatus.OPEN, company='OIL',
                  kind=DocumentKind.BILL, sap_doc_entry=501, request=None, save=mock.Mock())
    values.update(extra)
    return SimpleNamespace(**values)


class TheRecipientAndTheSender(SimpleTestCase):
    def test_only_the_recipient_dismisses_and_only_the_sender_withdraws(self):
        row = _assignment()
        with mock.patch.object(assignments, '_one', return_value=row):
            with self.assertRaisesRegex(assignments.AssignmentInvalid, 'sent to may dismiss'):
                assignments.dismiss(5, user=SimpleNamespace(pk=1))
            with self.assertRaisesRegex(assignments.AssignmentInvalid, 'who sent it may withdraw'):
                assignments.withdraw(5, user=SimpleNamespace(pk=21))
            self.assertEqual(assignments.dismiss(5, user=SimpleNamespace(pk=21)).status, AssignmentStatus.DISMISSED)

    def test_raising_the_request_closes_it(self):
        row = _assignment()
        advance = SimpleNamespace(company='OIL', documents=mock.Mock())
        advance.documents.filter.return_value.exists.return_value = True
        with mock.patch.object(assignments, '_one', return_value=row):
            assignments.link_request(5, advance, user=SimpleNamespace(pk=21))
        self.assertEqual((row.status, row.request), (AssignmentStatus.RAISED, advance))
        advance.documents.filter.assert_called_once_with(kind=DocumentKind.BILL, sap_doc_entry=501)

    def test_a_request_that_does_not_pay_it_leaves_it_open(self):
        row = _assignment()
        advance = SimpleNamespace(company='OIL', documents=mock.Mock())
        advance.documents.filter.return_value.exists.return_value = False
        with mock.patch.object(assignments, '_one', return_value=row):
            self.assertIsNone(assignments.link_request(5, advance, user=SimpleNamespace(pk=21)))
        self.assertEqual(row.status, AssignmentStatus.OPEN)

    def test_someone_elses_assignment_is_not_closed_by_your_request(self):
        row = _assignment(assigned_to_id=99)
        with mock.patch.object(assignments, '_one', return_value=row):
            self.assertIsNone(assignments.link_request(5, SimpleNamespace(company='OIL'), user=SimpleNamespace(pk=21)))
        self.assertEqual(row.status, AssignmentStatus.OPEN)
