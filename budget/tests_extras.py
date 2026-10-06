"""Budget approval: attachments, notifications, bulk approval and the reports.

No database and no SAP: rows are stand-ins, managers and HANA are patched.
"""
import contextlib
from datetime import date, datetime, timezone as tz
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from budget import views
from budget.models import ItemStatus
from budget.services import flow, notify, report, sap, sync

TEST = {'OIL': 'TEST_JIVO_OIL_HANADB'}


class FakeConn:
    def __init__(self, rows):
        self.rows, self.sql = rows, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        return self.rows


@override_settings(BUDGET_SAP_SCHEMAS=TEST)
class Attachments(SimpleTestCase):
    def test_a_draft_lists_its_atc1_files(self):
        conn = FakeConn([{'line': 1, 'name': 'Tax invoice', 'ext': 'pdf', 'date': datetime(2026, 8, 13)},
                         {'line': 2, 'name': '', 'ext': 'pdf', 'date': None}])
        with mock.patch.object(sap, 'HANAConnection', return_value=conn):
            got = sap.draft_attachments('OIL', 18, 54447)
        self.assertEqual(got, [{'line': 1, 'file_name': 'Tax invoice.pdf', 'date': date(2026, 8, 13)}])
        self.assertIn('"ODRF" D JOIN', conn.sql[0][0])
        self.assertEqual(conn.sql[0][1], [54447, 18])

    def test_a_payment_draft_reads_opdf(self):
        conn = FakeConn([])
        with mock.patch.object(sap, 'HANAConnection', return_value=conn):
            sap.draft_attachments('OIL', 46, 9)
        self.assertIn('"OPDF" D JOIN', conn.sql[0][0])

    def test_a_journal_voucher_reads_its_link(self):
        conn = FakeConn([{'link': '\\\\20.20.45.25\\Attachments_Oil\\JIVO_OIL\\Attachments\\bill merge.pdf'}])
        with mock.patch.object(sap, 'HANAConnection', return_value=conn):
            got = sap.draft_attachments('OIL', 28, 6409)
        self.assertEqual(got, [{'line': 0, 'file_name': 'bill merge.pdf', 'date': None}])

    def test_no_link_no_attachment(self):
        with mock.patch.object(sap, 'HANAConnection', return_value=FakeConn([{'link': None}])):
            self.assertEqual(sap.draft_attachments('OIL', 28, 1), [])

    def test_unreadable_sap_raises(self):
        with mock.patch.object(sap, 'HANAConnection', side_effect=ConnectionError('down')):
            with self.assertRaises(sap.SapUnavailable):
                sap.draft_attachments('OIL', 18, 1)


def item(pk=3, amount='100.00', waiting=None, **extra):
    draft = SimpleNamespace(get_obj_type_display=lambda: 'A/P invoice', doc_num=None, draft_entry=500 + pk,
                            card_name='Facebook India')
    values = dict(pk=pk, draft=draft, company='OIL', budget_code='Med_Mkt', amount=Decimal(amount),
                  waiting_since=waiting, status=ItemStatus.PENDING, current_user_id=21)
    return SimpleNamespace(**{**values, **extra})


class Notifications(SimpleTestCase):
    def test_one_item_reads_as_the_item(self):
        user = SimpleNamespace(pk=21, is_active=True)
        with mock.patch.object(notify, 'notify') as send:
            notify.new_items(user, [item()])
        kw = send.call_args.kwargs
        self.assertEqual(kw['event_type'], 'BUDGET_NEW_ITEMS')
        self.assertEqual(kw['message'], 'A/P invoice 503 · OIL · Med_Mkt · Facebook India · ₹100.00')

    def test_many_items_are_one_message(self):
        user = SimpleNamespace(pk=21, is_active=True)
        with mock.patch.object(notify, 'notify') as send:
            notify.new_items(user, [item(5, '200.00'), item(4, '50.50')])
        kw = send.call_args.kwargs
        self.assertEqual(send.call_count, 1)
        self.assertEqual(kw['title'], '2 budget approvals waiting for you')
        self.assertEqual(kw['entity'].pk, 4)  # the oldest item: a click opens something

    def test_the_reminder_points_at_the_oldest_waiting(self):
        user = SimpleNamespace(pk=21, is_active=True)
        old, new = (item(7, waiting=datetime(2026, 10, 1, tzinfo=tz.utc)),
                    item(8, waiting=datetime(2026, 10, 5, tzinfo=tz.utc)))
        with mock.patch.object(notify, 'notify') as send:
            notify.pending_reminder(user, [new, old])
        self.assertEqual(send.call_args.kwargs['entity'].pk, 7)
        self.assertEqual(send.call_args.kwargs['title'], '2 budget approvals pending')

    def test_nobody_inactive_and_nothing_to_say(self):
        with mock.patch.object(notify, 'notify') as send:
            notify.new_items(SimpleNamespace(pk=1, is_active=False), [item()])
            notify.pending_reminder(SimpleNamespace(pk=1, is_active=True), [])
        send.assert_not_called()

    def test_the_outcome_goes_to_earlier_actors_not_the_actor(self):
        logs = mock.Mock()
        logs.filter.return_value.exclude.return_value.values_list.return_value = [21, 22, 22]
        it = item(action_logs=logs)
        with mock.patch.object(notify, '_users', side_effect=lambda ids: sorted(ids)) as users, \
                mock.patch.object(notify, 'notify') as send:
            notify.decided(it, approved=False, actor=SimpleNamespace(pk=21), remarks='Not budgeted')
        users.assert_called_once_with({22})
        self.assertEqual(send.call_args.kwargs['event_type'], 'BUDGET_REJECTED')
        self.assertIn('Not budgeted', send.call_args.kwargs['message'])

    def test_a_failed_push_never_fails_the_decision(self):
        with mock.patch.object(notify, 'notify', side_effect=RuntimeError('expo down')):
            self.assertIsNone(notify.new_items(SimpleNamespace(pk=21, is_active=True), [item()]))

    def test_a_sync_announces_once_per_approver(self):
        items = [item(1), item(2), item(3, current_user_id=None)]
        items[1].current_user_id = 22
        users = mock.Mock()
        users.objects.filter.return_value = [SimpleNamespace(pk=21), SimpleNamespace(pk=22)]
        with mock.patch.object(sync, 'get_user_model', return_value=users), \
                mock.patch.object(sync.notify_service, 'new_items') as new_items:
            sync.announce(items)
        self.assertEqual(sorted((c.args[0].pk, [i.pk for i in c.args[1]]) for c in new_items.call_args_list),
                         [(21, [1]), (22, [2])])


#: The flow without its `@transaction.atomic` wrapper: no database here.
approve = flow.approve.__wrapped__
reject = flow.reject.__wrapped__


@contextlib.contextmanager
def _flow(locked, stages):
    with mock.patch.object(flow, '_locked', return_value=locked), mock.patch.object(flow, 'log'), \
            mock.patch.object(flow, 'effective_user_id', return_value=21), \
            mock.patch.object(flow, 'refresh_draft_status'), \
            mock.patch.object(flow.selection, 'stages_for', return_value=stages), \
            mock.patch.object(flow, 'notify_service') as notify_service:
        yield notify_service


class FlowNotifies(SimpleTestCase):
    STAGES = [SimpleNamespace(id=10, sequence=1), SimpleNamespace(id=11, sequence=2)]

    def _item(self, stage=10, seq=1):
        return SimpleNamespace(pk=3, status=ItemStatus.PENDING, current_stage_id=stage,
                               current_stage=SimpleNamespace(sequence=seq), workflow=object(), version=1,
                               draft=SimpleNamespace(), save=mock.Mock())

    def test_moving_on_tells_the_next_stage(self):
        with _flow(self._item(), self.STAGES) as sent:
            approve(self._item(), user=SimpleNamespace(pk=21))
        sent.stage_awaiting.assert_called_once()
        sent.decided.assert_not_called()

    def test_the_last_approval_tells_earlier_actors(self):
        user = SimpleNamespace(pk=21)
        it = self._item(11, 2)
        with _flow(it, self.STAGES) as sent:
            approve(it, user=user)
        sent.decided.assert_called_once_with(it, approved=True, actor=user)

    def test_auto_approval_has_no_actor(self):
        it = self._item(11, 2)
        with _flow(it, self.STAGES) as sent:
            approve(it, auto=True)
        self.assertIsNone(sent.decided.call_args.kwargs['actor'])

    def test_rejecting_tells_earlier_actors(self):
        user = SimpleNamespace(pk=21)
        it = self._item()
        with _flow(it, self.STAGES) as sent:
            reject(it, user=user, remarks='No')
        sent.decided.assert_called_once_with(it, approved=False, actor=user, remarks='No')


class Reports(SimpleTestCase):
    def test_the_month_is_the_documents_month(self):
        self.assertEqual(report._month('2026-12'), (date(2026, 12, 1), date(2027, 1, 1)))
        self.assertEqual(report._month('2026-02'), (date(2026, 2, 1), date(2026, 3, 1)))
        self.assertIsNone(report._month(''))
        for bad in ('2026-13', 'Oct', '2026'):
            with self.assertRaises(report.ReportInvalid):
                report._month(bad)

    def test_an_unknown_status_is_refused(self):
        with mock.patch.object(report.BudgetItem, 'objects'):
            with self.assertRaisesRegex(report.ReportInvalid, 'status'):
                report.items({'status': 'MAYBE'})

    def test_a_number_searches_the_draft_too(self):
        qs = mock.MagicMock()
        with mock.patch.object(report.BudgetItem, 'objects') as objects:
            objects.select_related.return_value.prefetch_related.return_value = qs
            report.items({'q': '54447'})
        condition = str(qs.filter.call_args.args[0])
        self.assertIn("('draft__draft_entry', 54447)", condition)
        self.assertIn("('draft__doc_num', 54447)", condition)


class BulkApproval(SimpleTestCase):
    def _post(self, body):
        request = APIRequestFactory().post('/api/budget/items/approve-bulk/', body, format='json')
        force_authenticate(request, user=SimpleNamespace(pk=21, is_authenticated=True, username='karanpreet'))
        with mock.patch.object(views.BulkApproveView, 'get_permissions', return_value=[]):
            return views.BulkApproveView.as_view()(request)

    def test_each_item_is_decided_on_its_own(self):
        ok_item, stale = SimpleNamespace(pk=1), SimpleNamespace(pk=2)
        loaded = {1: (ok_item, None), 2: (stale, None),
                  3: (None, SimpleNamespace(data={'message': 'This is not waiting for your decision.'}))}

        def approve(item, **kw):
            if item.pk == 2:
                raise flow.BudgetFlowError('It changed since you opened it. Reload it and try again.')
            return item, 'A'

        with mock.patch.object(views.BulkApproveView, '_load', side_effect=lambda req, pk: loaded[pk]), \
                mock.patch.object(views.flow_service, 'approve', side_effect=approve), \
                mock.patch.object(views.BulkApproveView, '_write', return_value=(True, '')):
            response = self._post({'items': [{'id': 1, 'version': 3}, {'id': 2, 'version': 1}, {'id': 3}],
                                   'remarks': 'ok'})
        data = response.data['data']
        self.assertEqual((data['approved'], data['refused']), (1, 2))
        self.assertEqual([r['ok'] for r in data['results']], [True, False, False])
        self.assertIn('changed since you opened it', data['results'][1]['message'])

    def test_too_many_or_none_is_refused(self):
        self.assertEqual(self._post({'items': []}).status_code, 400)
        self.assertEqual(self._post({'items': [{'id': i} for i in range(101)]}).status_code, 400)
