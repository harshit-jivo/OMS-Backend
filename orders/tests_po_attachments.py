"""A sales order's customer PO: attached on Add Sales, read by every desk.

Run with::

    python manage.py test orders.tests_po_attachments --settings=OMS.test_settings
"""
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from attachments.models import Attachment
from orders.models import Order, OrderStatus
from users.models import User, UserRole

PDF = b'%PDF-1.4 a customer purchase order'


def _user(username, role):
    role_obj, _ = UserRole.objects.get_or_create(name=role, defaults={'display_name': role})
    return User.objects.create_user(username=username, password='pw', name=username, role=role_obj)


def _pdf(name='po.pdf', content=PDF):
    return SimpleUploadedFile(name, content, content_type='application/pdf')


class OrderPoAttachmentTests(TestCase):

    def setUp(self):
        self.share = tempfile.mkdtemp()
        override = override_settings(ORDER_PO_ATTACHMENTS=self.share, PAYMENTS_SMB_USERNAME='')
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(shutil.rmtree, self.share, ignore_errors=True)

        self.creator = _user('po-creator', 'manager')
        self.other_manager = _user('po-other', 'manager')
        self.biller = _user('po-billing', 'billing')
        self.order = Order.objects.create(
            order_number='ORD-PO-1', created_by=self.creator,
            status=OrderStatus.objects.create(name='Billing', code='BILLING'))

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def _upload(self, user, *files):
        return self._client(user).post(
            reverse('order-attachments', args=[self.order.id]),
            {'file': list(files)}, format='multipart')

    def test_the_creator_attaches_a_po_and_the_billing_desk_can_read_it(self):
        res = self._upload(self.creator, _pdf())

        self.assertEqual(res.status_code, 201, res.content)
        listed = self._client(self.biller).get(reverse('order-attachments', args=[self.order.id]))
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()), 1)
        self.assertEqual(listed.json()[0]['attachment_type'], 'PURCHASE_ORDER')

        download = self._client(self.biller).get(listed.json()[0]['download_url'])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(b''.join(download.streaming_content), PDF)

    def test_another_manager_cannot_read_it(self):
        self._upload(self.creator, _pdf())
        attachment = Attachment.objects.get()

        self.assertEqual(self._client(self.other_manager).get(
            reverse('order-attachments', args=[self.order.id])).status_code, 403)
        self.assertEqual(self._client(self.other_manager).get(
            f'/api/payments/attachments/{attachment.id}/download/').status_code, 403)

    def test_only_the_creator_can_attach(self):
        res = self._upload(self.biller, _pdf())

        self.assertEqual(res.status_code, 403, res.content)
        self.assertFalse(Attachment.objects.exists())

    def test_a_bad_file_stores_nothing_even_beside_a_good_one(self):
        res = self._upload(self.creator, _pdf(), _pdf('fake.pdf', b'<html>not a pdf'))

        self.assertEqual(res.status_code, 400, res.content)
        self.assertFalse(Attachment.objects.exists())

    def test_a_closed_order_cannot_get_a_new_po(self):
        self.order.sap_created = True
        self.order.save(update_fields=['sap_created'])

        res = self._upload(self.creator, _pdf())

        self.assertEqual(res.status_code, 409, res.content)

    def test_the_creator_can_remove_a_po(self):
        self._upload(self.creator, _pdf())
        attachment = Attachment.objects.get()

        res = self._client(self.creator).delete(
            reverse('order-attachment-detail', args=[self.order.id, attachment.id]))

        self.assertEqual(res.status_code, 204, res.content)
        self.assertFalse(Attachment.objects.exists())
