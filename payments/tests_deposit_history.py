"""A deposit's timeline is reachable, and its remarks carry their author.

Deposits had no history endpoint while receipts did, so a deposit's progress
screen could only show the remark stored on the document — the creator's. Every
later note was written to PaymentStatusHistory and never read back by anything.

Two of the writers also left their rows untagged, so they defaulted to
STATUS_CHANGED, which the business timeline filters out. A rejection reason was
recorded and then hidden from the person who needed it most.
"""
from datetime import date
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from users.models import User, UserRole

from . import services
from .models import BankDeposit, PaymentStatusHistory
from .permissions import DEPOSIT_CREATE

Action = PaymentStatusHistory.Action


def _user(username, keys=()):
    role, _ = UserRole.objects.get_or_create(name='payments_and_deposit')
    return User.objects.create(
        username=username, name=username.title(), role=role,
        extra_pages=list(keys))


def _deposit(no, creator):
    return BankDeposit.objects.create(
        deposit_no=no, company='OIL', deposit_date=date(2026, 9, 1),
        collected_amount=Decimal('1000.00'), deposit_amount=Decimal('1000.00'),
        bank_code='HDFC', status=BankDeposit.Status.DRAFT,
        created_by=creator)


class DepositHistoryApiTests(TestCase):
    """The endpoint exists, is scoped, and reads oldest-first."""

    def setUp(self):
        self.user = _user('dh_creator', [DEPOSIT_CREATE])
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.deposit = _deposit('DEP-H-1', self.user)

    def _history(self, query=''):
        resp = self.client.get(
            reverse('bank-deposit-history', args=[self.deposit.pk]) + query)
        self.assertEqual(resp.status_code, 200)
        return resp.data['data']

    def test_the_route_exists_and_returns_rows(self):
        services.log_status(self.deposit, to_status='DRAFT', user=self.user,
                            action=Action.CREATED, reason='Deposit created.')
        rows = self._history()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['action'], 'CREATED')

    def test_a_remark_carries_the_username_that_wrote_it(self):
        """The whole point: a note is worthless without its author."""
        services.log_status(self.deposit, to_status='DRAFT', user=self.user,
                            action=Action.CREATED, reason='Cash counted twice.')
        row = self._history()[0]
        self.assertEqual(row['reason'], 'Cash counted twice.')
        self.assertEqual(row['changed_by_username'], 'dh_creator')
        self.assertEqual(row['performed_by'], 'dh_creator')

    def test_rows_read_oldest_first(self):
        for action, reason in ((Action.CREATED, 'First.'),
                               (Action.PENDING_APPROVAL, 'Second.'),
                               (Action.APPROVED, 'Third.')):
            services.log_status(self.deposit, to_status='DRAFT',
                                user=self.user, action=action, reason=reason)
        self.assertEqual([r['reason'] for r in self._history()],
                         ['First.', 'Second.', 'Third.'])

    def test_technical_rows_are_hidden_by_default(self):
        services.log_status(self.deposit, to_status='DRAFT', user=self.user,
                            action=Action.STATUS_CHANGED, reason='Noise.')
        services.log_status(self.deposit, to_status='DRAFT', user=self.user,
                            action=Action.APPROVED, reason='Signal.')
        self.assertEqual([r['reason'] for r in self._history()], ['Signal.'])

    def test_full_returns_the_technical_rows_too(self):
        services.log_status(self.deposit, to_status='DRAFT', user=self.user,
                            action=Action.STATUS_CHANGED, reason='Noise.')
        self.assertEqual(len(self._history('?full=true')), 1)

    def test_a_missing_deposit_is_404_not_500(self):
        resp = self.client.get(reverse('bank-deposit-history', args=[99999]))
        self.assertEqual(resp.status_code, 404)

    def test_history_belongs_only_to_its_own_deposit(self):
        other = _deposit('DEP-H-2', self.user)
        services.log_status(other, to_status='DRAFT', user=self.user,
                            action=Action.CREATED, reason='Other deposit.')
        services.log_status(self.deposit, to_status='DRAFT', user=self.user,
                            action=Action.CREATED, reason='This deposit.')
        self.assertEqual([r['reason'] for r in self._history()],
                         ['This deposit.'])


class DepositEventsAreTaggedTests(TestCase):
    """Rejection and cancellation name themselves instead of defaulting."""

    def setUp(self):
        self.user = _user('dh_actor', [DEPOSIT_CREATE])
        self.deposit = _deposit('DEP-H-10', self.user)

    def _actions(self):
        return list(
            PaymentStatusHistory.objects
            .filter(content_type=ContentType.objects.get_for_model(BankDeposit),
                    object_id=self.deposit.pk)
            .order_by('id').values_list('action', flat=True))

    def test_a_rejection_is_recorded_as_REJECTED(self):
        """Untagged it became STATUS_CHANGED, which the timeline filters out —
        so the approver's reason was written and then never shown."""
        services.log_status(self.deposit, from_status='PENDING_APPROVAL',
                            to_status='REJECTED', user=self.user,
                            action=Action.REJECTED,
                            reason='Slip does not match the amount.')
        self.assertEqual(self._actions(), ['REJECTED'])

    def test_a_cancellation_is_recorded_as_CANCELLED(self):
        services.log_status(self.deposit, from_status='PENDING_APPROVAL',
                            to_status='CANCELLED', user=self.user,
                            action=Action.CANCELLED,
                            reason='Cancelled by submitter.')
        self.assertEqual(self._actions(), ['CANCELLED'])

    def test_a_tagged_rejection_survives_the_default_filter(self):
        from .serializers import TECHNICAL_HISTORY_ACTIONS
        self.assertNotIn(Action.REJECTED, TECHNICAL_HISTORY_ACTIONS)
        self.assertNotIn(Action.CANCELLED, TECHNICAL_HISTORY_ACTIONS)


class DepositLineBankingGapTests(TestCase):
    """A deposit line carries when its receipt reached SAP.

    Without it the app could not answer "we booked this on the 1st, why was it
    banked on the 9th?" — the deposit knew its own date and the receipt's
    payment date, but not when the money actually posted.
    """

    def setUp(self):
        self.user = _user('dg_creator', [DEPOSIT_CREATE])

    def test_the_line_exposes_the_receipts_sap_posted_at(self):
        from django.utils import timezone

        from .models import BankDepositLine, PaymentMethodEntry, PaymentReceipt
        from .serializers import BankDepositSerializer

        posted = timezone.now()
        receipt = PaymentReceipt.objects.create(
            receipt_no='RCP-GAP-1', company='OIL', card_code='CUST1',
            payment_date=date(2026, 9, 1), total_amount=Decimal('500.00'),
            is_advance=True, sap_branch_id=1, sap_posted_at=posted,
            status=PaymentReceipt.Status.POSTED, created_by=self.user)
        PaymentMethodEntry.objects.create(
            receipt=receipt, method=PaymentMethodEntry.Method.UPI,
            upi_reference='UTR-GAP', amount=Decimal('500.00'))

        deposit = _deposit('DEP-GAP-1', self.user)
        BankDepositLine.objects.create(
            deposit=deposit, receipt=receipt, amount=Decimal('500.00'))

        line = BankDepositSerializer(deposit).data['lines'][0]
        self.assertIn('receipt_posted_at', line)
        self.assertIsNotNone(line['receipt_posted_at'])

    def test_an_unposted_receipt_reports_null_rather_than_a_guess(self):
        from .models import BankDepositLine, PaymentMethodEntry, PaymentReceipt
        from .serializers import BankDepositSerializer

        receipt = PaymentReceipt.objects.create(
            receipt_no='RCP-GAP-2', company='OIL', card_code='CUST1',
            payment_date=date(2026, 9, 1), total_amount=Decimal('500.00'),
            is_advance=True, sap_branch_id=1, sap_posted_at=None,
            status=PaymentReceipt.Status.DRAFT, created_by=self.user)
        PaymentMethodEntry.objects.create(
            receipt=receipt, method=PaymentMethodEntry.Method.UPI,
            upi_reference='UTR-GAP2', amount=Decimal('500.00'))

        deposit = _deposit('DEP-GAP-2', self.user)
        BankDepositLine.objects.create(
            deposit=deposit, receipt=receipt, amount=Decimal('500.00'))

        line = BankDepositSerializer(deposit).data['lines'][0]
        self.assertIsNone(line['receipt_posted_at'])


class DepositAttachmentsTests(TestCase):
    """Deposits serialize their slips — the app simply never asked for them."""

    def test_a_deposits_attachments_are_serialized(self):
        from .serializers import BankDepositSerializer

        user = _user('da_creator', [DEPOSIT_CREATE])
        deposit = _deposit('DEP-ATT-1', user)
        data = BankDepositSerializer(deposit).data
        self.assertIn('attachments', data)
        self.assertEqual(data['attachments'], [])


class DepositChangeDataTests(TestCase):
    """A deposit edit records WHICH fields moved, like a receipt edit does.

    Deposits logged `UPDATED` with no diff, so an approver reviewing a
    corrected deposit saw "Deposit updated." and had to guess whether the
    amount, the bank or the receipts had changed.
    """

    def setUp(self):
        self.user = _user('dc_editor', [DEPOSIT_CREATE])
        self.deposit = _deposit('DEP-CD-1', self.user)

    def _snapshot(self):
        from .serializers import BankDepositCreateSerializer
        return BankDepositCreateSerializer._audit_snapshot(self.deposit)

    def test_the_snapshot_covers_the_editable_fields(self):
        snap = self._snapshot()
        for field in ('deposit_date', 'deposit_amount', 'collected_amount',
                      'bank', 'slip_number', 'shortfall_reason', 'remarks',
                      'deposited_by', 'receipts'):
            self.assertIn(field, snap)

    def test_money_is_a_string_not_a_float(self):
        """A float would render 1000.00 as 999.9999999999999 in an audit row."""
        snap = self._snapshot()
        self.assertIsInstance(snap['deposit_amount'], str)
        self.assertIsInstance(snap['collected_amount'], str)

    def test_the_snapshot_excludes_sap_and_system_columns(self):
        """Only what a person can edit through the form belongs in the diff."""
        snap = self._snapshot()
        for banned in ('sap_doc_entry', 'sap_doc_num', 'sap_response',
                       'deposit_no', 'created_at', 'updated_at', 'status'):
            self.assertNotIn(banned, snap)

    def test_a_changed_field_is_reported_both_sides(self):
        from .serializers import PaymentReceiptCreateSerializer
        before = self._snapshot()
        after = dict(before, remarks='Corrected the slip number')
        diff = PaymentReceiptCreateSerializer._diff(before, after)
        self.assertEqual(diff['remarks']['new'], 'Corrected the slip number')
        self.assertEqual(set(diff), {'remarks'})

    def test_no_change_reports_null_not_an_empty_object(self):
        from .serializers import PaymentReceiptCreateSerializer
        before = self._snapshot()
        self.assertIsNone(
            PaymentReceiptCreateSerializer._diff(before, dict(before)))


class PerformedByNameTests(TestCase):
    """The timeline names the actor in full, not just by username.

    The history stores only `changed_by_username` — deliberately, so an audit
    row survives the account being deleted. The full name is resolved on read.
    """

    def setUp(self):
        self.user = _user('pbn_actor', [DEPOSIT_CREATE])
        self.user.name = 'Gagan Sharma'
        self.user.save(update_fields=['name'])
        self.deposit = _deposit('DEP-PBN-1', self.user)

    def _row(self):
        from .serializers import PaymentStatusHistorySerializer
        services.log_status(self.deposit, to_status='DRAFT', user=self.user,
                            action=Action.UPDATED, reason='Edited.')
        rows = list(PaymentStatusHistory.objects.filter(action=Action.UPDATED))
        return PaymentStatusHistorySerializer(rows, many=True).data[0]

    def test_the_full_name_is_resolved(self):
        row = self._row()
        self.assertEqual(row['changed_by_username'], 'pbn_actor')
        self.assertEqual(row['performed_by_name'], 'Gagan Sharma')

    def test_a_deleted_account_falls_back_to_the_username(self):
        """The row must still name who acted after the user is gone."""
        from .serializers import PaymentStatusHistorySerializer
        services.log_status(self.deposit, to_status='DRAFT', user=self.user,
                            action=Action.UPDATED, reason='Edited.')
        username = self.user.username
        self.user.delete()
        rows = list(PaymentStatusHistory.objects.filter(action=Action.UPDATED))
        data = PaymentStatusHistorySerializer(rows, many=True).data[0]
        self.assertEqual(data['performed_by_name'], username)

    def test_a_system_row_says_System(self):
        from .serializers import PaymentStatusHistorySerializer
        services.log_status(self.deposit, to_status='POSTED', user=None,
                            action=Action.SAP_POSTED, reason='Posted.')
        row = PaymentStatusHistory.objects.get(action=Action.SAP_POSTED)
        data = PaymentStatusHistorySerializer([row], many=True).data[0]
        self.assertEqual(data['performed_by_name'], 'System')

    def test_names_resolve_in_one_query_for_the_whole_list(self):
        """Per-row resolution would be an N+1 on every timeline read."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from .serializers import PaymentStatusHistorySerializer
        for i in range(6):
            services.log_status(self.deposit, to_status='DRAFT',
                                user=self.user, action=Action.UPDATED,
                                reason=f'Edit {i}.')
        rows = list(PaymentStatusHistory.objects.filter(action=Action.UPDATED))
        with CaptureQueriesContext(connection) as ctx:
            data = PaymentStatusHistorySerializer(rows, many=True).data
        self.assertEqual(len(data), 6)
        self.assertLessEqual(len(ctx), 1)


class SourceGlAccountTests(TestCase):
    """A deposit names BOTH sides of the movement it represents.

    The header used to show `bank_gl_account` twice — once inside
    `bank_display_name`, once appended as "GL <n>" — so a reader saw the
    destination stated twice and never learned where the money came from.
    """

    def setUp(self):
        self.user = _user('sgl_creator', [DEPOSIT_CREATE])
        self.deposit = _deposit('DEP-SGL-1', self.user)

    def _data(self):
        from .serializers import BankDepositSerializer
        return BankDepositSerializer(self.deposit).data

    def test_the_field_is_exposed(self):
        self.assertIn('source_gl_account', self._data())

    def _cash_mapping(self, gl_account):
        from .models import PaymentMethodMapping
        PaymentMethodMapping.objects.update_or_create(
            company='OIL', payment_method='CASH',
            defaults={'bank_key': '', 'gl_account': gl_account,
                      'is_active': True})

    def test_it_resolves_the_cash_mapping_account(self):
        """The deposit empties the drawer the CASH receipts filled."""
        self._cash_mapping('1105001')
        self.assertEqual(self._data()['source_gl_account'], '1105001')

    def test_it_follows_the_cash_account_when_that_changes(self):
        """There is no separate override any more — one drawer, one account.

        A second configurable value could only ever disagree with the cash
        G/L, and then the clearing account would not net to zero.
        """
        self._cash_mapping('1105003')
        self.assertEqual(self._data()['source_gl_account'], '1105003')

    def test_an_unmapped_company_reports_null_not_a_blank(self):
        """The UI omits the row rather than printing an empty account."""
        from .models import PaymentMethodMapping
        PaymentMethodMapping.objects.filter(
            company='OIL', payment_method='CASH').delete()
        self.assertIsNone(self._data()['source_gl_account'])

    def test_an_inactive_mapping_is_ignored(self):
        """Deactivating the mapping must not leave a stale account in use."""
        from .models import PaymentMethodMapping
        self._cash_mapping('1105001')
        PaymentMethodMapping.objects.filter(
            company='OIL', payment_method='CASH').update(is_active=False)
        self.assertIsNone(self._data()['source_gl_account'])


class AttachmentUploadIsLoggedTests(TestCase):
    """An upload lands on the document's timeline, not only in its own table.

    Uploading arrives through a separate endpoint and never touches the
    document's `update()`, so nothing wrote a history row — an approver could
    see a new file on the page with no record of who added it or when.
    """

    def setUp(self):
        self.user = _user('att_uploader', [DEPOSIT_CREATE])
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _rows(self, deposit):
        return PaymentStatusHistory.objects.filter(
            content_type=ContentType.objects.get_for_model(BankDeposit),
            object_id=deposit.pk, action=Action.UPDATED)

    def test_the_diff_names_the_attachment_that_was_added(self):
        """Shaped exactly like any other edit, so the card renders it as one."""
        deposit = _deposit('DEP-ATT-LOG', self.user)
        services.log_status(
            deposit, from_status=deposit.status, to_status=deposit.status,
            user=self.user, action=Action.UPDATED,
            change_data={'attachments': {
                'old': [],
                'new': ['Bank deposit slip'],
            }},
            reason='Attached Bank deposit slip.')

        row = self._rows(deposit).get()
        self.assertEqual(row.change_data['attachments']['old'], [])
        self.assertEqual(row.change_data['attachments']['new'],
                         ['Bank deposit slip'])
        self.assertEqual(row.changed_by_username, 'att_uploader')

    def test_a_second_upload_keeps_the_first_in_the_old_list(self):
        """The previous files are the 'before', so nothing looks removed."""
        deposit = _deposit('DEP-ATT-LOG2', self.user)
        services.log_status(
            deposit, from_status=deposit.status, to_status=deposit.status,
            user=self.user, action=Action.UPDATED,
            change_data={'attachments': {
                'old': ['Bank deposit slip'],
                'new': ['Bank deposit slip', 'Deposit receipt'],
            }},
            reason='Attached Deposit receipt.')

        change = self._rows(deposit).get().change_data['attachments']
        self.assertIn('Bank deposit slip', change['old'])
        self.assertEqual(len(change['new']), 2)
