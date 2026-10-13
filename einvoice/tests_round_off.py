"""The IRN carries SAP's document rounding, so its total matches the invoice.

The Sales Invoice page sends `Rounding: tYES` + `RoundingDiffAmount`; SAP's
invoice total then includes it, and the IRN must too (ValDtls.RndOffAmt) or
NIC's TotInvVal check and the SAP total disagree.

Run with::

    python manage.py test einvoice.tests_round_off --settings=OMS.test_settings
"""
from django.test import SimpleTestCase

from einvoice.mapping import build_irn
from einvoice.validation import validate_invoice


def _invoice(**header):
    return {
        "DocNum": 1001,
        "DocDate": "2026-10-13",
        "VATRegNum": "03AAACJ1234A1Z5",
        "CardName": "Buyer",
        "EWayBillDetails": {"BillToStateGSTCode": "03", "BillToGSTIN": "03AAACB1234B1Z5"},
        "DocumentLines": [{
            "ItemCode": "FG1", "ItemDescription": "Oil", "Quantity": 3,
            "Price": 33.33, "LineTotal": 99.99, "TaxTotal": 5.0,
            "TaxPercentagePerRow": 5, "HSNEntry": 1,
        }],
        **header,
    }


class RoundOffTests(SimpleTestCase):

    def test_rounding_is_carried_into_the_irn_total(self):
        irn = build_irn(_invoice(Rounding="tYES", RoundingDiffAmount=0.01), hsn_map={1: "15079010"})

        self.assertEqual(irn["ValDtls"]["RndOffAmt"], 0.01)
        self.assertEqual(irn["ValDtls"]["TotInvVal"], 105.0)
        codes = {e.get("code") for e in validate_invoice(irn)}
        self.assertNotIn("TOTINV_MISMATCH", codes)
        self.assertNotIn("ROUNDOFF_RANGE", codes)

    def test_no_rounding_leaves_the_total_alone(self):
        irn = build_irn(_invoice(Rounding="tNO", RoundingDiffAmount=0.01), hsn_map={1: "15079010"})

        self.assertNotIn("RndOffAmt", irn["ValDtls"])
        self.assertEqual(irn["ValDtls"]["TotInvVal"], 104.99)
