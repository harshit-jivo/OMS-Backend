"""Tests for the OMS-generated SAP-style receipt PDF endpoint.

GET /api/payments/receipts/{id}/sap-report/ — posted-only, access-controlled,
returns application/pdf built from OMS data (no SAP call).
"""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from payments.models import (
    CashDenomination,
    PaymentAllocation,
    PaymentMethodEntry,
    PaymentReceipt,
)


def _pdf_text(pdf_bytes):
    """Extract visible text from a ReportLab PDF for assertions.

    ReportLab FlateDecode-compresses (and ASCII85-wraps) the content stream, so
    the drawn text is NOT readable in the raw bytes. Decompress every stream and
    pull the strings inside text-show operators ((...)Tj and [...]TJ).
    """
    import base64
    import re
    import zlib

    out = []
    # ReportLab wraps each content stream as ASCII85 (…~>) over Flate. Grab each
    # ASCII85 block, a85-decode (non-adobe), then inflate, then read the shown
    # strings inside (...) text operators.
    for block in re.findall(rb"(G[!-u][!-uz\s]*?)~>", pdf_bytes, re.DOTALL):
        try:
            data = zlib.decompress(base64.a85decode(block, adobe=False))
        except Exception:  # noqa: BLE001
            continue
        text = data.decode("latin-1", errors="ignore")
        for s in re.findall(r"\(((?:[^()\\]|\\.)*)\)", text):
            out.append(s.replace("\\(", "(").replace("\\)", ")").replace("\\\\", "\\"))
    return "\n".join(out)


class ReceiptPdfEndpointTests(APITestCase):
    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.owner = User.objects.create(username="pdf_owner", name="Owner")
        cls.stranger = User.objects.create(username="pdf_stranger", name="Stranger")

    def _receipt(self, *, status=PaymentReceipt.Status.POSTED, no="RCP-PDF-1",
                 sap_doc_entry=20793, sap_doc_num=826246503, sap_trans_id=212174,
                 owner=None, is_advance=False, total_amount=Decimal("1.00")):
        return PaymentReceipt.objects.create(
            receipt_no=no, company="OIL", card_code="CUSTA000729",
            card_name="Test Party Pvt Ltd", payment_date=date(2026, 8, 13),
            total_amount=total_amount, remarks="CHEQUE | RCP-OIL-20260813-000001 | CHQ 1470 | ICICI | 2026-08-13",
            status=status, sap_doc_entry=sap_doc_entry, sap_doc_num=sap_doc_num,
            sap_trans_id=sap_trans_id, is_advance=is_advance,
            created_by=owner or self.owner,
        )

    def _get(self, receipt, user=None):
        self.client.force_authenticate(user or self.owner)
        return self.client.get(f"/api/payments/receipts/{receipt.id}/sap-report/")

    # 1 — POSTED → 200 application/pdf, %PDF header
    def test_posted_returns_pdf(self):
        r = self._receipt()
        PaymentMethodEntry.objects.create(receipt=r, method="CHEQUE",
                                          amount=Decimal("1.00"),
                                          cheque_number="1470", bank_name="ICICI",
                                          cheque_date=date(2026, 8, 13))
        res = self._get(r)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "application/pdf")
        self.assertIn("attachment", res["Content-Disposition"])
        self.assertIn(f"Receipt-{r.receipt_no}.pdf", res["Content-Disposition"])
        body = b"".join(res.streaming_content) if res.streaming else res.content
        self.assertTrue(body.startswith(b"%PDF"))

    # 2-5 — non-posted statuses → 409 with the exact message
    def test_non_posted_statuses_return_409(self):
        msg = "has not been posted successfully"
        for st in [PaymentReceipt.Status.DRAFT,
                   PaymentReceipt.Status.PENDING_APPROVAL,
                   PaymentReceipt.Status.PENDING_ERROR,
                   PaymentReceipt.Status.REJECTED]:
            r = self._receipt(status=st, no=f"RCP-{st}", sap_doc_entry=None,
                              sap_doc_num=None, sap_trans_id=None)
            res = self._get(r)
            self.assertEqual(res.status_code, 409, st)
            self.assertIn(msg, str(res.data))

    def test_posted_but_no_doc_entry_returns_409(self):
        r = self._receipt(no="RCP-NODE", sap_doc_entry=None)
        res = self._get(r)
        self.assertEqual(res.status_code, 409)

    # 6 — unauthorized user → 403
    def test_stranger_forbidden(self):
        r = self._receipt(no="RCP-PRIV")
        res = self._get(r, user=self.stranger)
        self.assertEqual(res.status_code, 403)

    def test_requires_auth(self):
        r = self._receipt(no="RCP-AUTH")
        res = self.client.get(f"/api/payments/receipts/{r.id}/sap-report/")
        self.assertIn(res.status_code, (401, 403))

    # 7 — PDF content includes the key identifiers
    def test_pdf_content_has_identifiers(self):
        r = self._receipt(no="RCP-CONTENT")
        PaymentMethodEntry.objects.create(receipt=r, method="CASH",
                                          amount=Decimal("1.00"))
        res = self._get(r)
        body = b"".join(res.streaming_content) if res.streaming else res.content
        text = _pdf_text(body)
        for token in [r.receipt_no, "Test Party Pvt Ltd", str(r.sap_doc_num),
                      str(r.sap_doc_entry), str(r.sap_trans_id)]:
            self.assertIn(token, text, f"missing {token!r} in PDF")

    # 8 — CASH payment section + denominations
    def test_cash_with_denominations(self):
        r = self._receipt(no="RCP-CASH", total_amount=Decimal("700.00"))
        entry = PaymentMethodEntry.objects.create(receipt=r, method="CASH",
                                                  amount=Decimal("700.00"))
        CashDenomination.objects.create(entry=entry, denomination=500, quantity=1)
        CashDenomination.objects.create(entry=entry, denomination=200, quantity=1)
        res = self._get(r)
        text = _pdf_text(b"".join(res.streaming_content) if res.streaming else res.content)
        self.assertIn("Cash", text)
        self.assertIn("Denomination", text)

    # 9 — UPI shows the UTR reference
    def test_upi_shows_reference(self):
        r = self._receipt(no="RCP-UPI")
        PaymentMethodEntry.objects.create(receipt=r, method="UPI",
                                          amount=Decimal("1.00"),
                                          upi_reference="UTR123456789")
        res = self._get(r)
        text = _pdf_text(b"".join(res.streaming_content) if res.streaming else res.content)
        self.assertIn("UPI", text)
        self.assertIn("UTR123456789", text)

    # 10 — CHEQUE shows number, bank, date
    def test_cheque_shows_details(self):
        r = self._receipt(no="RCP-CHQ")
        PaymentMethodEntry.objects.create(receipt=r, method="CHEQUE",
                                          amount=Decimal("1.00"),
                                          cheque_number="1470", bank_name="ICICI",
                                          cheque_date=date(2026, 8, 13))
        res = self._get(r)
        text = _pdf_text(b"".join(res.streaming_content) if res.streaming else res.content)
        self.assertIn("Cheque", text)
        self.assertIn("1470", text)
        self.assertIn("ICICI", text)

    # 11 — advance payment: shows "advance — no invoice attached", no row invented
    def test_advance_shows_no_invoice_note(self):
        r = self._receipt(no="RCP-ADV", is_advance=True)
        PaymentMethodEntry.objects.create(receipt=r, method="UPI",
                                          amount=Decimal("1.00"))
        res = self._get(r)  # SAP unreachable in tests → OMS fallback, no allocation
        text = _pdf_text(b"".join(res.streaming_content) if res.streaming else res.content)
        self.assertIn("advance payment", text.lower())
        self.assertIn("no invoice attached", text.lower())

    # SAP is the source of truth for everything EXCEPT the customer name/code,
    # which come from OMS (SAP holds the bank account, not the customer, on an
    # account-type receipt). Remarks come from SAP's ORCT.
    def test_fields_from_sap_except_customer(self):
        from unittest import mock

        r = self._receipt(no="RCP-ADDR")  # OMS card_name = "Test Party Pvt Ltd"
        PaymentMethodEntry.objects.create(receipt=r, method="CASH",
                                          amount=Decimal("1.00"))
        PaymentAllocation.objects.create(
            receipt=r, sap_doc_entry=66995, sap_doc_num=626050474,
            invoice_date=date(2026, 7, 15), invoice_total=Decimal("91800.00"),
            amount_applied=Decimal("1.00"))
        sap_doc = {
            "DocType": "rAccount",                         # bank receipt
            "CardCode": "2201101",
            "CardName": "INDIAN BANK CC A/C 7007270527",  # the BANK, not customer
            "Address": "OPPOSITE GOVT HIGH SCHOOL\rGURGAON-122001\rIN",
            "DocNum": 826248149,
            "DocDate": "2026-06-15T00:00:00Z",
            "DocCurrency": "INR",
            "CashSum": 91800.0,
            "Remarks": "SAP exact remark from ORCT",
            "BPLName": "FACTORY",
            "PaymentInvoices": [
                {"DocNum": 626050474, "SumApplied": 91800.0, "InvoiceType": "it_Invoice"},
                {"DocNum": 111, "SumApplied": -91800.0, "InvoiceType": "it_Receipt"},
            ],
        }
        with mock.patch("payments.receipt_pdf._fetch_sap_document", return_value=sap_doc):
            self.client.force_authenticate(self.owner)
            res = self.client.get(f"/api/payments/receipts/{r.id}/sap-report/")
        text = _pdf_text(b"".join(res.streaming_content) if res.streaming else res.content)
        # rAccount: SAP CardName/CardCode are the BANK, so BOTH the customer name
        # and code come from OMS (the real customer), never the bank code:
        self.assertIn("Test Party Pvt Ltd", text)
        self.assertNotIn("INDIAN BANK CC", text)
        self.assertIn("CUSTA000729", text)                # OMS customer code
        self.assertNotIn("2201101", text)                 # NOT the bank G/L code
        # Everything else from SAP:
        self.assertIn("GURGAON-122001", text)             # SAP Address
        self.assertIn("826248149", text)                  # SAP DocNum
        self.assertIn("FACTORY", text)                    # SAP branch
        self.assertIn("15-Jun-2026", text)                # SAP DocDate
        self.assertIn("91,800", text)                     # SAP tender/total
        self.assertIn("Cash", text)                       # SAP method (CashSum)
        self.assertIn("626050474", text)                  # SAP invoice line
        self.assertIn("15-Jul-2026", text)                # invoice date from allocation
        self.assertNotIn("111", text)                     # negative receipt line excluded
        # Remarks from SAP's ORCT, NOT the OMS-stored one:
        self.assertIn("SAP exact remark from ORCT", text)
        self.assertNotIn("CHQ 1470", text)

    # For a customer-type receipt, SAP's ORCT CardName IS the customer → use it.
    def test_customer_type_uses_sap_card_name(self):
        from unittest import mock

        r = self._receipt(no="RCP-CUST")  # OMS card_name = "Test Party Pvt Ltd"
        PaymentMethodEntry.objects.create(receipt=r, method="CASH",
                                          amount=Decimal("1.00"))
        sap_doc = {
            "DocType": "rCustomer",                        # customer receipt
            "CardCode": "CUSTA000546",
            "CardName": "HARPREET SINGH CASH SALE",        # the real customer
            "DocNum": 826248149,
            "DocDate": "2026-06-15T00:00:00Z",
            "DocCurrency": "INR",
            "CashSum": 1.0,
            "Remarks": "ORCT remark",
            "PaymentInvoices": [],
        }
        with mock.patch("payments.receipt_pdf._fetch_sap_document", return_value=sap_doc):
            self.client.force_authenticate(self.owner)
            res = self.client.get(f"/api/payments/receipts/{r.id}/sap-report/")
        text = _pdf_text(b"".join(res.streaming_content) if res.streaming else res.content)
        self.assertIn("HARPREET SINGH CASH SALE", text)   # SAP CardName (ORCT)
        self.assertNotIn("Test Party Pvt Ltd", text)      # OMS name NOT used
        self.assertIn("CUSTA000546", text)                # SAP CardCode

    # OCRD party master supplies the billing address block + payment terms.
    def test_ocrd_address_and_terms_render(self):
        from unittest import mock

        r = self._receipt(no="RCP-OCRD")
        PaymentMethodEntry.objects.create(receipt=r, method="CASH",
                                          amount=Decimal("1.00"))
        sap_doc = {"DocType": "rCustomer", "CardCode": "CUSTA001127",
                   "CardName": "SHRI GANESH TRADING COMPANY", "DocNum": 826248145,
                   "DocDate": "2026-08-19T00:00:00Z", "DocCurrency": "INR",
                   "CashSum": 1.0, "Remarks": "x", "PaymentInvoices": []}
        party = {"card_code": "CUSTA001127", "card_name": "SHRI GANESH TRADING COMPANY",
                 "address": "MOHALLA INAYATGANJ OPP. OF B.O.B\nPILIBHIT 262001\nIN",
                 "payment_terms": "ADVANCE/CASH/0 DAYS"}
        with mock.patch("payments.receipt_pdf._fetch_sap_document", return_value=sap_doc), \
                mock.patch("payments.receipt_pdf._fetch_party_master", return_value=party):
            self.client.force_authenticate(self.owner)
            res = self.client.get(f"/api/payments/receipts/{r.id}/sap-report/")
        text = _pdf_text(b"".join(res.streaming_content) if res.streaming else res.content)
        self.assertIn("MOHALLA INAYATGANJ", text)         # OCRD billing address
        self.assertIn("PILIBHIT 262001", text)
        self.assertIn("ADVANCE/CASH/0 DAYS", text)        # OCRD payment terms

    # SAP unreachable → receipt still renders from OMS data, never errors
    def test_sap_down_still_renders(self):
        from unittest import mock

        r = self._receipt(no="RCP-NOSAP")
        PaymentMethodEntry.objects.create(receipt=r, method="CASH",
                                          amount=Decimal("1.00"))
        with mock.patch("payments.receipt_pdf._fetch_sap_document", return_value=None):
            res = self._get(r)
        self.assertEqual(res.status_code, 200)
        body = b"".join(res.streaming_content) if res.streaming else res.content
        self.assertTrue(body.startswith(b"%PDF"))
        text = _pdf_text(body)
        self.assertIn("Test Party Pvt Ltd", text)  # OMS fallback name

    # PNG — ?format=png returns an inline image the app can show
    def test_png_format_returns_image(self):
        r = self._receipt(no="RCP-PNG")
        PaymentMethodEntry.objects.create(receipt=r, method="CASH",
                                          amount=Decimal("1.00"))
        self.client.force_authenticate(self.owner)
        res = self.client.get(f"/api/payments/receipts/{r.id}/sap-report/?as=png")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res["Content-Type"], "image/png")
        self.assertIn("inline", res["Content-Disposition"])
        body = b"".join(res.streaming_content) if res.streaming else res.content
        self.assertTrue(body.startswith(b"\x89PNG"))

    # PNG — non-posted still 409
    def test_png_non_posted_returns_409(self):
        r = self._receipt(no="RCP-PNG-DRAFT", status=PaymentReceipt.Status.DRAFT,
                          sap_doc_entry=None)
        self.client.force_authenticate(self.owner)
        res = self.client.get(f"/api/payments/receipts/{r.id}/sap-report/?as=png")
        self.assertEqual(res.status_code, 409)

    # 12 — invoice allocation appears
    def test_invoice_allocation_appears(self):
        r = self._receipt(no="RCP-INV")
        PaymentMethodEntry.objects.create(receipt=r, method="CASH",
                                          amount=Decimal("1.00"))
        PaymentAllocation.objects.create(
            receipt=r, sap_doc_entry=66995, sap_doc_num=555001,
            invoice_total=Decimal("1666560.00"), amount_applied=Decimal("1.00"))
        res = self._get(r)
        text = _pdf_text(b"".join(res.streaming_content) if res.streaming else res.content)
        self.assertIn("555001", text)


class ReceiptPdfBuilderTests(APITestCase):
    """Directly exercise build_receipt_pdf — no HTTP, no SAP, no DB mutation."""

    def test_builder_returns_pdf_bytes_and_does_not_mutate(self):
        from payments.receipt_pdf import build_receipt_pdf

        User = get_user_model()
        owner = User.objects.create(username="b_owner", name="B")
        r = PaymentReceipt.objects.create(
            receipt_no="RCP-BUILD", company="OIL", card_code="C1",
            card_name="Party", payment_date=date(2026, 8, 13),
            total_amount=Decimal("1.00"), status=PaymentReceipt.Status.POSTED,
            sap_doc_entry=20793, sap_doc_num=826246503, sap_trans_id=212174,
            created_by=owner)
        PaymentMethodEntry.objects.create(receipt=r, method="CASH",
                                          amount=Decimal("1.00"))
        before = (r.status, r.sap_doc_entry, r.sap_response)
        pdf = build_receipt_pdf(r)
        self.assertIsInstance(pdf, bytes)
        self.assertTrue(pdf.startswith(b"%PDF"))
        r.refresh_from_db()
        self.assertEqual((r.status, r.sap_doc_entry, r.sap_response), before)
