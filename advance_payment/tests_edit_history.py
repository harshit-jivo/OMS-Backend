"""What an edit leaves in the request's history: each change, Was -> Now.

The EDITED log row is what the request page shows under "Edited", so its
values are stored as the page reads them — department NAMES, the partner's
code and name, documents by number — not ids nobody can read back.
"""
from decimal import Decimal

from advance_payment.models import (
    Department, FlowStatus, LogAction, RequestDocument, RequestFlow, RequestLog, RequestStatus, SubDepartment,
)
from advance_payment.services import flow as flow_service
from advance_payment.services import requests as request_service
from advance_payment.services.requests import CleanRequest
from advance_payment.tests_desk import DeskFixture


def doc(entry, amount, num=None):
    return {'kind': 'BILL', 'sap_doc_entry': entry, 'sap_doc_num': str(num or entry),
            'original_amount': Decimal('100000'), 'paid_amount': Decimal('0'),
            'open_amount': Decimal('100000'), 'amount': Decimal(amount)}


class EditHistory(DeskFixture):
    def setUp(self):
        super().setUp()
        self.advance = self.request(at=self.my_stage)
        for d in (doc(10256, '40000'), doc(10271, '20000')):
            RequestDocument.objects.create(request=self.advance, **d)

    def edit(self, fields, documents):
        return request_service.update(self.advance, CleanRequest(fields=fields, documents=documents),
                                      user=self.creator)

    def test_records_each_changed_field_with_its_old_and_new_value(self):
        changes = self.edit({'amount': Decimal('1500'), 'remarks': 'Mobilisation'},
                            [doc(10256, '40000'), doc(10271, '20000')])
        self.assertEqual(changes['amount'], {'old': '1000.00', 'new': '1500.00'})
        self.assertEqual(changes['remarks'], {'old': None, 'new': 'Mobilisation'})
        self.assertNotIn('priority', changes)  # unchanged: not listed
        self.assertNotIn('documents', changes)

    def test_names_the_department_rather_than_its_id(self):
        ops = Department.objects.create(name='Operations')
        sub = SubDepartment.objects.create(department=ops, name='Plant')
        changes = self.edit({'department': ops, 'sub_department': sub},
                            [doc(10256, '40000'), doc(10271, '20000')])
        self.assertEqual(changes['department'], {'old': 'Finance', 'new': 'Operations'})
        self.assertEqual(changes['sub_department'], {'old': None, 'new': 'Plant'})

    def test_says_which_documents_were_added_removed_or_re_amounted(self):
        changes = self.edit({}, [doc(10256, '45000'), doc(10300, '5000')])
        self.assertEqual(changes['documents'], {
            'added': [{'doc': 'A/P invoice 10300', 'amount': '5000.00'}],
            'removed': [{'doc': 'A/P invoice 10271', 'amount': '20000.00'}],
            'changed': [{'doc': 'A/P invoice 10256', 'old': '40000.00', 'new': '45000.00'}],
        })

    def test_the_creators_edit_is_logged_and_nothing_logged_when_nothing_changed(self):
        # Returned to its creator: editing it neither routes nor resubmits it.
        advance = self.request(status=RequestStatus.RETURNED, flow_status=FlowStatus.RETURNED)
        RequestDocument.objects.filter(request=self.advance).update(request=advance)
        self.advance = advance
        flow_service.edit(self.advance, CleanRequest(fields={'priority': 'HIGH'},
                                                     documents=[doc(10256, '40000'), doc(10271, '20000')]),
                          user=self.creator)
        row = self.advance.logs.get(action=LogAction.EDITED)
        self.assertEqual(row.actor, self.creator)
        self.assertEqual(row.data['priority']['new'], 'HIGH')

        flow_service.edit(self.advance, CleanRequest(fields={'priority': 'HIGH'},
                                                     documents=[doc(10256, '40000'), doc(10271, '20000')]),
                          user=self.creator)
        self.assertEqual(self.advance.logs.filter(action=LogAction.EDITED).count(), 1)


class PaymentDetailsHistory(DeskFixture):
    """Payment's bank details: each save logs what changed, and a typed account is flagged."""

    def setUp(self):
        super().setUp()
        self.advance = self.request(at=self.their_stage)  # at Payment Approval
        self.payer = self.other

    def save(self, data, *, from_sap):
        from unittest import mock
        RequestFlow.objects.filter(request=self.advance).update(current_role='PAYMENT')
        with mock.patch.object(flow_service, '_acting',
                               return_value=RequestFlow.objects.get(request=self.advance)), \
                mock.patch.object(flow_service, '_from_sap', return_value=from_sap), \
                mock.patch.object(flow_service, '_token_ok', return_value=True):
            flow_service.save_payout(self.advance, data, user=self.payer)
        return self.advance.logs.filter(action=LogAction.PAYOUT_UPDATED).order_by('-id').first()

    def payout(self, account, ifsc, amount='1000'):
        return {'beneficiary_name': 'A VENDOR', 'to_account_number': account, 'to_ifsc': ifsc,
                'lines': [{'method': 'NEFT', 'amount': amount, 'from_account': '1104106',
                           'from_account_label': 'HDFC BANK-50200012345678'}]}

    def test_the_first_save_logs_each_detail_as_new(self):
        row = self.save(self.payout('50100234567812', 'HDFC0001234'), from_sap=True)
        self.assertEqual(row.data['to_account'], {'old': None, 'new': 'XXXXXXXXXX7812'})
        self.assertEqual(row.data['account_source'], {'old': None, 'new': 'From SAP'})
        self.assertEqual(row.data['payment_method_1']['new'],
                         'NEFT · ₹1,000.00 · from HDFC BANK-50200012345678')
        self.assertFalse(row.data['manual_account'])

    def test_a_changed_account_says_what_it_was_and_is_and_flags_a_typed_one(self):
        self.save(self.payout('50100234567812', 'HDFC0001234'), from_sap=True)
        row = self.save(self.payout('12345678901', 'SBIN0001234'), from_sap=False)
        self.assertEqual(row.data['to_account'], {'old': 'XXXXXXXXXX7812', 'new': 'XXXXXXX8901'})
        self.assertEqual(row.data['to_ifsc'], {'old': 'HDFC0001234', 'new': 'SBIN0001234'})
        self.assertEqual(row.data['account_source'], {'old': 'From SAP', 'new': 'Typed by hand'})
        self.assertTrue(row.data['manual_account'])
        self.assertTrue(row.data['manual_new'])
        self.assertNotIn('beneficiary_name', row.data)  # unchanged: not listed

    def test_a_split_change_alone_is_logged_without_touching_the_account(self):
        self.save(self.payout('50100234567812', 'HDFC0001234'), from_sap=True)
        row = self.save(self.payout('50100234567812', 'HDFC0001234', amount='900'), from_sap=True)
        self.assertEqual(set(row.data) - {'manual_account', 'manual_new', 'account_last4'},
                         {'payment_method_1'})
        self.assertIn('₹900.00', row.data['payment_method_1']['new'])


class AccountDetailsFromPaymentOn(DeskFixture):
    """The payee's account reaches Payment and later stages only.

    Fixture route: `my_stage` (an approval, `self.me`) then `their_stage`
    (Payment Approval, `self.other`).
    """

    def setUp(self):
        super().setUp()
        self.their_stage.name = 'Payment Approval'
        self.advance = self.request(at=self.their_stage)
        RequestFlow.objects.filter(request=self.advance).update(current_role='PAYMENT')
        from advance_payment.models import Payout
        Payout.objects.create(request=self.advance, beneficiary_name='A VENDOR',
                              to_account_number='50100234567812', to_ifsc='HDFC0001234',
                              updated_by=self.other)
        self.decide(self.advance, LogAction.APPROVED, by=self.me)
        RequestLog.objects.create(request=self.advance, action=LogAction.PAYOUT_UPDATED, actor=self.other,
                                  data={'to_account': {'old': None, 'new': 'XXXXXXXXXX7812'},
                                        'manual_account': True})

    def detail(self, user):
        from unittest import mock
        from advance_payment.serializers import request_data
        advance = type(self.advance).objects.get(pk=self.advance.pk)
        with mock.patch.object(flow_service, 'roles',
                               return_value=[(self.my_stage, 'APPROVAL'), (self.their_stage, 'PAYMENT')]):
            return request_data(advance, user=user, detail=True)

    def test_the_payment_user_sees_the_account_and_its_change_log(self):
        out = self.detail(self.other)
        self.assertTrue(out['can']['see_account'])
        self.assertEqual(out['payout']['to_account_number'], '50100234567812')
        row = next(r for r in out['logs'] if r['action'] == 'PAYOUT_UPDATED')
        self.assertTrue(row['data']['manual_account'])

    def test_an_approver_before_payment_does_not(self):
        out = self.detail(self.me)
        self.assertFalse(out['can']['see_account'])
        self.assertIsNone(out['payout'])
        row = next(r for r in out['logs'] if r['action'] == 'PAYOUT_UPDATED')
        self.assertIsNone(row['data'])  # it happened; what it holds is not theirs

    def test_nor_does_the_creator(self):
        out = self.detail(self.creator)
        self.assertFalse(out['can']['see_account'])
        self.assertIsNone(out['payout'])
