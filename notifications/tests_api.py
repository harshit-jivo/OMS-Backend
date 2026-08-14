"""Read-API tests for the reusable notification framework.

Proves the API is per-recipient scoped (a user sees only their OWN
notifications), surfaces the generic entity + a coarse module label, and
supports mark-read / mark-all-read / unread-count.
"""

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from rest_framework.test import APITestCase

from notifications.models import Notification
from payments.models import BankDeposit, PaymentReceipt


class FrameworkNotificationApiTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        from datetime import date

        from users.models import Company

        User = get_user_model()
        cls.company = Company.objects.create(name="Api Co")
        cls.me = User.objects.create(username="api_me", name="Me", company=cls.company)
        cls.other = User.objects.create(username="api_other", name="Other", company=cls.company)

        cls.receipt = PaymentReceipt.objects.create(
            receipt_no="RC-API", company="OIL", card_code="C1",
            payment_date=date.today(), total_amount=100, created_by=cls.me)
        cls.deposit = BankDeposit.objects.create(
            deposit_no="DEP-API", company="OIL", deposit_date=date.today(),
            collected_amount=100, deposit_amount=100, created_by=cls.me)

        ct_r = ContentType.objects.get_for_model(PaymentReceipt)
        ct_d = ContentType.objects.get_for_model(BankDeposit)
        # Two notifications for me (one payment, one deposit) + one for someone else.
        cls.n_pay = Notification.objects.create(
            user=cls.me, company=cls.company, event_type="PAYMENT_SUBMITTED",
            title="Payment approval required", message="Payment RC-API needs approval.",
            content_type=ct_r, object_id=cls.receipt.id)
        cls.n_dep = Notification.objects.create(
            user=cls.me, company=cls.company, event_type="DEPOSIT_SUBMITTED",
            title="Deposit approval required", message="Deposit DEP-API needs approval.",
            content_type=ct_d, object_id=cls.deposit.id)
        Notification.objects.create(
            user=cls.other, company=cls.company, event_type="PAYMENT_SUBMITTED",
            title="x", message="not mine", content_type=ct_r, object_id=cls.receipt.id)

    def test_list_is_scoped_to_caller(self):
        self.client.force_authenticate(self.me)
        res = self.client.get("/api/notifications/")
        self.assertEqual(res.status_code, 200)
        ids = {row["id"] for row in res.data}
        self.assertEqual(ids, {self.n_pay.id, self.n_dep.id})  # never the other user's

    def test_list_surfaces_generic_entity_and_module(self):
        self.client.force_authenticate(self.me)
        res = self.client.get("/api/notifications/")
        by_event = {row["event_type"]: row for row in res.data}
        pay = by_event["PAYMENT_SUBMITTED"]
        self.assertEqual(pay["entity_type"], "paymentreceipt")
        self.assertEqual(pay["entity_id"], self.receipt.id)
        self.assertEqual(pay["module"], "payments")
        dep = by_event["DEPOSIT_SUBMITTED"]
        self.assertEqual(dep["entity_type"], "bankdeposit")
        self.assertEqual(dep["module"], "deposits")

    def test_unread_count(self):
        self.client.force_authenticate(self.me)
        res = self.client.get("/api/notifications/unread-count/")
        self.assertEqual(res.data["unread"], 2)

    def test_mark_one_read(self):
        self.client.force_authenticate(self.me)
        res = self.client.patch(f"/api/notifications/{self.n_pay.id}/")
        self.assertEqual(res.status_code, 200)
        self.n_pay.refresh_from_db()
        self.assertTrue(self.n_pay.is_read)

    def test_cannot_mark_others_notification(self):
        self.client.force_authenticate(self.other)
        res = self.client.patch(f"/api/notifications/{self.n_pay.id}/")
        self.assertEqual(res.status_code, 404)  # not visible to a different user

    def test_mark_all_read(self):
        self.client.force_authenticate(self.me)
        res = self.client.post("/api/notifications/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            Notification.objects.filter(user=self.me, is_read=False).count(), 0)
        # The other user's unread notification is untouched.
        self.assertEqual(
            Notification.objects.filter(user=self.other, is_read=False).count(), 1)

    def test_requires_auth(self):
        res = self.client.get("/api/notifications/")
        self.assertIn(res.status_code, (401, 403))
