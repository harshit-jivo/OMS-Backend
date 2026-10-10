"""The request's files: onto the company's SAP share, then onto its SAP payment."""
import io
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings

from advance_payment.models import VoucherStatus
from advance_payment.services import sap_attachments
from advance_payment.services import voucher as voucher_service
from payments import sap_client

FILE_SERVICE = dict(FILE_UPLOAD_URL='http://fs', FILE_UPLOAD_TOKEN='fu_api_test',
                    FILE_UPLOAD_FOLDERS={'OIL': 3, 'BEVERAGES': 4, 'MART': 5},
                    SAP_ATTACHMENT_SOURCE_PATHS={'OIL': '/mnt/Oil', 'BEVERAGES': '/mnt/Bev', 'MART': '/mnt/Mart'})


def row(pk=1, name='PO 221026015.pdf', **fields):
    f = SimpleNamespace(pk=pk, name=name, share_file_id=None, share_name='', share_folder=None, share_error='',
                        sap_attachment_entry=None, uploaded_by=SimpleNamespace(email='ravi@jivo.in'),
                        file=SimpleNamespace(open=lambda mode: io.BytesIO(b'%PDF')), saved=[])
    f.__dict__.update(fields)
    f.save = lambda update_fields: f.saved.append(tuple(update_fields))
    return f


def answer(status=200, body=None):
    return SimpleNamespace(status_code=status, json=lambda: body)


@override_settings(**FILE_SERVICE)
class OnTheCompanysShare(SimpleTestCase):
    def test_goes_to_the_request_companys_folder_and_keeps_the_services_name(self):
        stored = {'status': 'success', 'data': {'files': [{'id': 900, 'stored_name': 'PO 221026015_v2.pdf'}],
                                                'failures': []}}
        f = row()
        with mock.patch.object(sap_attachments.requests, 'post', return_value=answer(body=stored)) as post:
            self.assertTrue(sap_attachments.share_file(f, 'BEVERAGES'))
        self.assertEqual(post.call_args.kwargs['data'], {'folder_id': 4})
        self.assertEqual(post.call_args.kwargs['headers']['X-Uploader'], 'ravi@jivo.in')
        self.assertEqual((f.share_file_id, f.share_name, f.share_folder, f.share_error),
                         (900, 'PO 221026015_v2.pdf', 4, ''))

    def test_a_partial_answer_is_a_failure_recorded_on_the_file(self):
        partial = {'status': 'partial', 'data': {'files': [], 'failures': [
            {'file': 'x.exe', 'code': 'FILE_TYPE_NOT_ALLOWED', 'message': 'This file extension is not allowed.'}]}}
        f = row(name='x.exe')
        with mock.patch.object(sap_attachments.requests, 'post', return_value=answer(body=partial)):
            self.assertFalse(sap_attachments.share_file(f, 'OIL'))
        self.assertEqual(f.share_error, 'This file extension is not allowed.')
        self.assertIsNone(f.share_file_id)

    def test_already_there_is_not_sent_again_but_a_new_company_moves_it(self):
        f = row(share_file_id=900, share_name='PO.pdf', share_folder=3, sap_attachment_entry=55)
        stored = {'status': 'success', 'data': {'files': [{'id': 901, 'stored_name': 'PO.pdf'}], 'failures': []}}
        with mock.patch.object(sap_attachments.requests, 'post', return_value=answer(body=stored)) as post, \
                mock.patch.object(sap_attachments.requests, 'delete') as delete:
            self.assertTrue(sap_attachments.share_file(f, 'OIL'))
            post.assert_not_called()
            self.assertTrue(sap_attachments.share_file(f, 'MART'))
        self.assertEqual((f.share_file_id, f.share_folder, f.sap_attachment_entry), (901, 5, None))
        self.assertIn('/files/900', delete.call_args.args[0])  # the copy on Oil's share goes

    @override_settings(FILE_UPLOAD_TOKEN='')
    def test_does_nothing_when_the_service_is_not_configured(self):
        self.assertEqual(sap_attachments.share_request_files(SimpleNamespace(company='OIL')), (0, 0))


@override_settings(**FILE_SERVICE)
class OnTheSapPayment(SimpleTestCase):
    advance = SimpleNamespace(company='OIL', request_no='AP-2026-0015')

    def test_makes_one_attachment_from_the_shared_files(self):
        files = [row(1, share_file_id=900, share_name='PO 221026015.pdf', share_folder=3),
                 row(2, share_file_id=901, share_name='Contract mail', share_folder=3)]
        with mock.patch.object(sap_attachments, '_ready', return_value=(files, [])), \
                mock.patch.object(sap_client, 'request', return_value=(201, {'AbsoluteEntry': 7001})) as sap, \
                mock.patch.object(sap_attachments.RequestFile.objects, 'filter') as marked:
            self.assertEqual(sap_attachments.for_payment(self.advance, 'TEST_DB'), (7001, ''))
        method, path = sap.call_args.args
        self.assertEqual((method, path, sap.call_args.kwargs['company_db']), ('POST', '/Attachments2', 'TEST_DB'))
        self.assertEqual(sap.call_args.kwargs['json_body']['Attachments2_Lines'], [
            {'SourcePath': '/mnt/Oil', 'FileName': 'PO 221026015', 'FileExtension': 'pdf', 'Override': 'tYES'},
            {'SourcePath': '/mnt/Oil', 'FileName': 'Contract mail', 'FileExtension': '', 'Override': 'tYES'},
        ])
        marked.return_value.update.assert_called_once_with(sap_attachment_entry=7001)

    def test_a_retry_reuses_the_attachment_already_made(self):
        files = [row(1, share_file_id=900, share_name='a.pdf', share_folder=3, sap_attachment_entry=7001)]
        with mock.patch.object(sap_attachments, '_ready', return_value=(files, [])), \
                mock.patch.object(sap_client, 'request') as sap:
            self.assertEqual(sap_attachments.for_payment(self.advance, 'TEST_DB'), (7001, ''))
        sap.assert_not_called()

    def test_a_refusal_is_a_reason_not_an_error(self):
        files = [row(1, share_file_id=900, share_name='a.pdf', share_folder=3)]
        refused = sap_client.SapError('400', status_code=400, sap_code='-5002', payload={'error': {
            'code': '-5002', 'message': 'Attachments folder not defined'}})
        with mock.patch.object(sap_attachments, '_ready', return_value=(files, [])), \
                mock.patch.object(sap_client, 'request', side_effect=refused):
            entry, problem = sap_attachments.for_payment(self.advance, 'TEST_DB')
        self.assertIsNone(entry)
        self.assertIn('Attachments folder not defined', problem)

    def test_no_files_no_attachment(self):
        with mock.patch.object(sap_attachments, '_ready', return_value=([], [])):
            self.assertEqual(sap_attachments.for_payment(self.advance, 'TEST_DB'), (None, ''))

    def test_the_payment_is_never_stopped_by_its_files(self):
        with mock.patch.object(sap_attachments, 'for_payment', side_effect=RuntimeError('share down')):
            entry, problem = voucher_service._attachment(self.advance, 'TEST_DB')
        self.assertIsNone(entry)
        self.assertIn('share down', problem)


@override_settings(**FILE_SERVICE)
class AfterPosting(SimpleTestCase):
    advance = SimpleNamespace(company='OIL', request_no='AP-2026-0015')

    def voucher(self, entry=None):
        v = SimpleNamespace(status=VoucherStatus.POSTED, sap_doc_entry=30001, attachment_entry=entry,
                            attachment_error='was down', saved=[])
        v.save = lambda update_fields: v.saved.append(tuple(update_fields))
        return v

    def attach(self, voucher, files):
        with mock.patch.object(voucher_service, 'live', return_value=voucher), \
                mock.patch.object(voucher_service, '_company_db', return_value='TEST_DB'), \
                mock.patch.object(sap_attachments, '_ready', return_value=(files, [])), \
                mock.patch.object(sap_attachments.RequestFile.objects, 'filter'), \
                mock.patch('advance_payment.services.flow.log'), \
                mock.patch.object(sap_client, 'request', return_value=(201, {'AbsoluteEntry': 7002})) as sap:
            result = sap_attachments.attach_after_posting(self.advance, user=None)
        return result, [(c.args[0], c.args[1], c.kwargs['json_body']) for c in sap.call_args_list]

    def test_a_payment_posted_without_files_gets_an_attachment_set_on_it(self):
        v = self.voucher()
        result, calls = self.attach(v, [row(1, share_file_id=900, share_name='a.pdf', share_folder=3)])
        self.assertEqual(result, (1, ''))
        self.assertEqual([c[:2] for c in calls], [('POST', '/Attachments2'), ('PATCH', '/VendorPayments(30001)')])
        self.assertEqual(calls[1][2], {'AttachmentEntry': 7002})
        self.assertEqual((v.attachment_entry, v.attachment_error), (7002, ''))

    def test_files_added_after_posting_are_added_as_lines(self):
        v = self.voucher(entry=7001)
        files = [row(1, share_file_id=900, share_name='a.pdf', share_folder=3, sap_attachment_entry=7001),
                 row(2, share_file_id=905, share_name='UTR proof.png', share_folder=3)]
        result, calls = self.attach(v, files)
        self.assertEqual(result, (1, ''))
        self.assertEqual([c[:2] for c in calls], [('PATCH', '/Attachments2(7001)')])
        self.assertEqual(calls[0][2]['Attachments2_Lines'][0]['FileName'], 'UTR proof')

    def test_not_posted_yet(self):
        with mock.patch.object(voucher_service, 'live', return_value=None):
            count, problem = sap_attachments.attach_after_posting(self.advance, user=None)
        self.assertEqual(count, 0)
        self.assertIn('not posted', problem)
