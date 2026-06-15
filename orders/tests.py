from django.test import SimpleTestCase

from .views import _get_rate_approval_reason


class RateApprovalReasonTests(SimpleTestCase):
    def test_price_list_basic_zero_requires_rate_approval_for_any_basic_price(self):
        item = {"item_name": "Mustard Oil"}

        self.assertIsNotNone(_get_rate_approval_reason(item, 0, 0))
        self.assertIsNotNone(_get_rate_approval_reason(item, 0, 500))

    def test_basic_price_below_price_list_basic_requires_rate_approval(self):
        item = {"item_name": "Mustard Oil"}

        self.assertIsNotNone(_get_rate_approval_reason(item, 1000, 900))

    def test_valid_basic_price_does_not_require_rate_approval(self):
        item = {"item_name": "Mustard Oil"}

        self.assertIsNone(_get_rate_approval_reason(item, 1000, 1000))
