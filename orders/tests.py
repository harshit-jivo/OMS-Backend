from django.test import SimpleTestCase

from .views import _get_rate_approval_reason


class RateApprovalReasonTests(SimpleTestCase):
    def test_basic_price_zero_requires_rate_approval_for_any_market_price(self):
        item = {"item_name": "Mustard Oil"}

        self.assertIsNotNone(_get_rate_approval_reason(item, 0, 0))
        self.assertIsNotNone(_get_rate_approval_reason(item, 0, 500))

    def test_market_price_below_basic_price_requires_rate_approval(self):
        item = {"item_name": "Mustard Oil"}

        self.assertIsNotNone(_get_rate_approval_reason(item, 1000, 900))

    def test_valid_market_price_does_not_require_rate_approval(self):
        item = {"item_name": "Mustard Oil"}

        self.assertIsNone(_get_rate_approval_reason(item, 1000, 1000))
