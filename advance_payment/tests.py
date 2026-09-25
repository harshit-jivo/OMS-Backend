"""Payment proofs: reading any bank's statement or advice, and finding the UTR.

No database and no SAP: `payment_proof` is pure text work, `proof_reader` is
fed files built here, and the view's SAP lookups are patched.
"""
import io
from types import SimpleNamespace
from unittest import mock

import openpyxl
import pymupdf
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from advance_payment import views
from advance_payment.services import payment_proof, proof_reader

#: A statement in the common shape (HDFC-like layout), with several rails.
STATEMENT = [
    "Date   Narration   Chq/Ref No   Value Dt   Withdrawal Amt   Deposit Amt   Closing Balance",
    "22/09/26   UPI-SWIGGY-SWIGGY@ICICI-ICIC0DC0099-626512340000-FOOD   0000626512340000   22/09/26   450.00   12,34,567.89",
    "23/09/26   NEFT DR-HDFC0001452-GODAMWALE TRADING-NETBANK, MUM-HDFCN52026092312345678-INV 2024-25/DL086"
    "   23/09/26   2,88,746.00   9,45,821.89",
    "23/09/26   IMPS-626612345678-RCM LOGISTICS-ICIC-XXXXXXXX5689-ADV   23/09/26   20,358.00   9,25,463.89",
    "24/09/26   RTGS DR-SBIN0001234-JINDAL BROTHERS-SBINR52026092400012345   24/09/26   5,00,000.00   4,25,463.89",
]


class FindsTheReference(SimpleTestCase):
    def test_picks_the_statement_row_of_this_payment(self):
        result = payment_proof.extract(
            STATEMENT, amount="288746", accounts=["50200057911744"], invoices=["2024-25/DL086"])
        self.assertEqual(result["kind"], "statement")
        self.assertEqual(result["utr"], "HDFCN52026092312345678")
        self.assertEqual(result["channel"], "NEFT")
        self.assertEqual(result["amount"], 288746.0)
        self.assertEqual(result["date"], "23/09/26")
        self.assertIs(result["checks"]["amount"], True)
        self.assertIs(result["checks"]["invoice"], True)
        # The narration does not print the payee's account: not a failure.
        self.assertIsNone(result["checks"]["account"])
        self.assertEqual(result["references_found"], 4)

    def test_imps_rrn_with_a_masked_account(self):
        result = payment_proof.extract(STATEMENT, amount="20358", accounts=["777705145689"])
        self.assertEqual((result["utr"], result["channel"]), ("626612345678", "IMPS"))
        self.assertIs(result["checks"]["account"], True)  # XXXXXXXX5689

    def test_rtgs(self):
        result = payment_proof.extract(STATEMENT, amount="500000")
        self.assertEqual((result["utr"], result["channel"]), ("SBINR52026092400012345", "RTGS"))

    def test_an_advice_spreads_its_evidence_over_the_page(self):
        advice = [
            "Transaction Successful",
            "Amount   Rs. 20,358.00",
            "Beneficiary A/c No.   777705145689",
            "IFSC   ICIC0003662",
            "UTR No.   ICICN52026092400098765",
            "Remarks   LDH005266",
            "Date   24-Sep-2026 11:42",
        ]
        result = payment_proof.extract(
            advice, amount="20358", accounts=["777705145689"], invoices=["LDH005266"])
        self.assertEqual(result["kind"], "advice")
        self.assertEqual(result["utr"], "ICICN52026092400098765")
        self.assertEqual(result["amount"], 20358.0)
        self.assertEqual(result["checks"], {
            "amount": True, "account": True, "account_other": None, "invoice": True})

    def test_a_upi_screenshot_puts_the_value_under_its_label(self):
        screenshot = ["Paid to", "RAHUL SHARMA", "₹15,000", "Completed", "UPI transaction ID",
                      "626712345678", "To: HDFC Bank XXXX7812", "From: ICICI Bank 2322"]
        result = payment_proof.extract(screenshot, amount="15000", accounts=["50100234567812"])
        self.assertEqual((result["utr"], result["channel"]), ("626712345678", "UPI"))
        self.assertIs(result["checks"]["amount"], True)
        self.assertIs(result["checks"]["account"], True)

    def test_says_when_the_proof_shows_another_amount_and_another_account(self):
        proof = ["UTR No: HDFCR52026092312345678", "Amount: 2,00,000.00", "Beneficiary A/c: 629305042322"]
        result = payment_proof.extract(
            proof, amount="288746", accounts=["50200057911744"], other_accounts=["629305042322"])
        self.assertEqual(result["utr"], "HDFCR52026092312345678")
        self.assertIs(result["checks"]["amount"], False)
        self.assertIs(result["checks"]["account"], False)
        self.assertEqual(result["checks"]["account_other"], "629305042322")

    def test_never_reads_an_account_or_ifsc_as_a_reference(self):
        rows = ["IMPS to 629305042322 IFSC ICIC0006293", "Amount 1,000.00"]
        # 629305042322 is 12 digits, exactly an RRN's shape: known accounts are kept out.
        result = payment_proof.extract(rows, amount="1000", own_accounts=["629305042322"])
        self.assertIsNone(result["utr"])

    def test_a_bare_twelve_digit_number_is_not_an_rrn_without_imps_or_upi(self):
        self.assertEqual(payment_proof.find_references(["Customer ID 626612345678"]), [])

    def test_nothing_found(self):
        result = payment_proof.extract(["Opening balance 1,00,000.00"], amount="100")
        self.assertIsNone(result["utr"])
        self.assertEqual(result["candidates"], [])


def _pdf_with(lines):
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for text in lines:
        page.insert_text((40, y), text, fontsize=9)
        y += 14
    data = doc.tobytes()
    doc.close()
    return data


class ReadsEveryFormat(SimpleTestCase):
    def test_pdf_with_text_is_read_directly_and_keeps_rows_together(self):
        doc = pymupdf.open()
        page = doc.new_page()
        # Columns written as separate words, as a statement PDF places them.
        page.insert_text((40, 100), "23/09/26", fontsize=9)
        page.insert_text((120, 100), "NEFT/HDFCN52026092312345678/GODAMWALE", fontsize=9)
        page.insert_text((450, 100), "2,88,746.00", fontsize=9)
        page.insert_text((40, 114), "24/09/26   opening balance carried", fontsize=9)
        data = doc.tobytes()
        result = proof_reader.read(data, "statement.pdf")
        self.assertEqual(result["source"], "pdf-text")
        self.assertEqual(result["ocr_pages"], 0)
        self.assertIn("NEFT/HDFCN52026092312345678/GODAMWALE", result["rows"][0])
        self.assertIn("2,88,746.00", result["rows"][0])
        self.assertTrue(result["rows"][0].startswith("23/09/26"))

    def test_xlsx_keeps_account_numbers_whole(self):
        book = openpyxl.Workbook()
        sheet = book.active
        sheet.append(["Date", "Narration", "Account", "Debit"])
        sheet.append(["23/09/26", "IMPS-626612345678-RCM", 777705145689, 20358])
        buf = io.BytesIO()
        book.save(buf)
        result = proof_reader.read(buf.getvalue(), "statement.xlsx")
        self.assertEqual(result["source"], "excel")
        self.assertEqual(result["rows"][1], "23/09/26 | IMPS-626612345678-RCM | 777705145689 | 20358.00")

    def test_html_saved_as_xls(self):
        html = (b"<html><body><table><tr><th>Date</th><th>Narration</th></tr>"
                b"<tr><td>23/09/26</td><td>UPI/626712345678/RAHUL</td></tr></table></body></html>")
        result = proof_reader.read(html, "Statement.xls")
        self.assertEqual(result["rows"], ["Date | Narration", "23/09/26 | UPI/626712345678/RAHUL"])

    def test_csv(self):
        result = proof_reader.read(b"Date,Narration,Amount\n23/09/26,NEFT-SBIN126267123456-X,1500.00\n", "s.csv")
        self.assertEqual(result["source"], "csv")
        self.assertEqual(result["rows"][1], "23/09/26 | NEFT-SBIN126267123456-X | 1500.00")

    @override_settings(OCR_SERVICE_URL="")
    def test_a_photo_needs_the_ocr_service(self):
        with self.assertRaises(proof_reader.OcrUnavailable):
            proof_reader.read(b"\xff\xd8\xff\xe0 fake jpeg", "screenshot.jpg")

    @override_settings(OCR_SERVICE_URL="http://ocr.test")
    def test_a_photo_is_read_by_the_ocr_service_by_row(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {"pages": [{"rows": ["UTR No. ICICN52026092400098765"], "text": "x"}]}
        with mock.patch.object(proof_reader.requests, "post", return_value=response) as post:
            result = proof_reader.read(b"\x89PNG....", "advice.png")
        self.assertEqual(result, {"source": "ocr", "rows": ["UTR No. ICICN52026092400098765"],
                                  "pages": 1, "ocr_pages": 1})
        self.assertEqual(post.call_args.args[0], "http://ocr.test/ocr")

    def test_refuses_what_it_cannot_read(self):
        with self.assertRaisesMessage(proof_reader.ProofUnreadable, "empty"):
            proof_reader.read(b"", "x.pdf")
        with self.assertRaisesMessage(proof_reader.ProofUnreadable, "HEIC"):
            proof_reader.read(b"....ftypheic", "IMG_0001.HEIC")
        with self.assertRaisesMessage(proof_reader.ProofUnreadable, "Upload a PDF"):
            proof_reader.read(b"hello", "notes.docx")

    def test_password_protected_pdf_says_what_to_do(self):
        doc = pymupdf.open()
        doc.new_page().insert_text((40, 72), "secret statement text here for the test")
        data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
        with self.assertRaisesMessage(proof_reader.ProofUnreadable, "password"):
            proof_reader.read(data, "statement.pdf")


@mock.patch.object(views.ap_perms.CanViewLookups, "has_permission", return_value=True)
@mock.patch.object(views.sap_service, "house_banks",
                   return_value=[{"account_number": "629305042322"}])
@mock.patch.object(views.sap_service, "partner_bank_accounts",
                   return_value=([{"account_number": "777705145689"}, {"account_number": "11112222333"}],
                                 "777705145689"))
class TheEndpoint(SimpleTestCase):
    def _post(self, **fields):
        request = APIRequestFactory().post("/api/advance-payments/payment-proof/", fields, format="multipart")
        force_authenticate(request, user=SimpleNamespace(pk=1, id=1, is_authenticated=True, is_active=True))
        return views.PaymentProofView.as_view()(request)

    def test_reads_a_pdf_and_checks_it_against_the_payment(self, _banks, _house, _perm):
        pdf = _pdf_with(STATEMENT)
        response = self._post(
            file=SimpleUploadedFile("statement.pdf", pdf, content_type="application/pdf"),
            company="OIL", amount="20358", to_account="777705145689", card_code="VENDA000102",
            invoices="LDH005266")
        self.assertEqual(response.status_code, 200)
        data = response.data["data"]
        self.assertEqual(data["utr"], "626612345678")
        self.assertEqual(data["source"], "pdf-text")
        self.assertIs(data["checks"]["amount"], True)
        self.assertIs(data["checks"]["account"], True)
        _banks.assert_called_once_with("OIL", "VENDA000102")

    def test_needs_a_file_and_a_company(self, *_):
        self.assertEqual(self._post(company="OIL").status_code, 400)
        response = self._post(file=SimpleUploadedFile("a.csv", b"x,y\n"), company="NOPE")
        self.assertEqual(response.status_code, 400)

    @override_settings(OCR_SERVICE_URL="")
    def test_a_photo_without_ocr_is_503(self, *_):
        response = self._post(file=SimpleUploadedFile("a.jpg", b"\xff\xd8\xff\xe0 x"), company="MART")
        self.assertEqual(response.status_code, 503)


class TheEmployeeMaster(SimpleTestCase):
    """The Add Employee page's endpoint: admins only, and what it accepts."""

    def _request(self, method, user, data=None):
        factory = APIRequestFactory()
        request = (factory.post("/api/advance-payments/employee-master/", data or {}, format="json")
                   if method == "post" else factory.get("/api/advance-payments/employee-master/"))
        force_authenticate(request, user=user)
        return views.EmployeeMasterView.as_view()(request)

    def test_only_administrators(self):
        user = SimpleNamespace(pk=1, id=1, is_authenticated=True, is_active=True)
        with mock.patch.object(views.IsAdminRole, "has_permission", return_value=False):
            self.assertEqual(self._request("get", user).status_code, 403)
            self.assertEqual(self._request("post", user, {"employee_code": "X"}).status_code, 403)

    def _serializer(self, **data):
        from advance_payment.serializers import EmployeeSerializer
        clash = mock.Mock()
        clash.exists.return_value = False
        with mock.patch.object(views.Employee.objects, "filter", return_value=clash):
            serializer = EmployeeSerializer(data=data)
            valid = serializer.is_valid()
        return valid, serializer

    def test_cleans_what_is_typed(self):
        valid, s = self._serializer(employee_code=" jwpl3100 ", employee_name="  Asha   Rani ",
                                    role=3, email="", phone="98111 22334", gender="")
        self.assertTrue(valid, s.errors)
        self.assertEqual(s.validated_data["employee_code"], "JWPL3100")
        self.assertEqual(s.validated_data["employee_name"], "Asha Rani")
        self.assertIsNone(s.validated_data["email"])
        self.assertIsNone(s.validated_data["gender"])

    def test_needs_code_name_and_a_known_role(self):
        valid, s = self._serializer(employee_code="", employee_name="", role=9)
        self.assertFalse(valid)
        self.assertEqual(set(s.errors), {"employee_code", "employee_name", "role"})

    def test_refuses_a_bad_phone_or_email(self):
        valid, s = self._serializer(employee_code="A1", employee_name="A", role=1,
                                    phone="abc", email="not-an-email")
        self.assertFalse(valid)
        self.assertEqual(set(s.errors), {"phone", "email"})

    def test_refuses_a_code_that_exists(self):
        from advance_payment.serializers import EmployeeSerializer
        clash = mock.Mock()
        clash.exists.return_value = True
        with mock.patch.object(views.Employee.objects, "filter", return_value=clash):
            s = EmployeeSerializer(data={"employee_code": "jwpl0115", "employee_name": "X", "role": 3})
            self.assertFalse(s.is_valid())
        self.assertIn("JWPL0115 already exists", str(s.errors["employee_code"]))


class _FakeRows(list):
    """Enough of a queryset for EmployeeDirectoryView: filter() and iteration."""

    def filter(self, *args, **kwargs):
        rows = list(self)
        if 'role__in' in kwargs:
            rows = [e for e in rows if e.role in kwargs['role__in']]
        return _FakeRows(rows)


def _employee(code, name, role):
    return SimpleNamespace(employee_code=code, employee_name=name, role=role, designation=None,
                           get_role_display=lambda: {1: 'HOD', 2: 'Sub-HOD', 3: 'Executive'}[role])


@mock.patch.object(views.ap_perms.CanViewLookups, "has_permission", return_value=True)
class TheEmployeeDirectory(SimpleTestCase):
    """What the request form's Employee and Ownership pickers read."""

    ROWS = _FakeRows([_employee("JWPL0115", "Arvinder", 1), _employee("JWPL0030", "Preshit Singh", 2),
                      _employee("JWPL2846", "Prabhdit Singh", 3), _employee("JWPL3100", "Asha Rani", 3)])

    def _get(self, query):
        request = APIRequestFactory().get(f"/api/advance-payments/employee-directory/?{query}")
        force_authenticate(request, user=SimpleNamespace(pk=1, id=1, is_authenticated=True, is_active=True))
        with mock.patch.object(views.Employee.objects, "active", return_value=self.ROWS):
            return views.EmployeeDirectoryView.as_view()(request)

    def test_ownership_asks_for_hods_and_sub_hods(self, _perm):
        response = self._get("roles=1,2")
        self.assertEqual(response.status_code, 200)
        names = [(r["employee_name"], r["role_label"]) for r in response.data["data"]["results"]]
        self.assertEqual(names, [("Arvinder", "HOD"), ("Preshit Singh", "Sub-HOD")])

    def test_not_in_sap_leaves_out_everyone_with_an_advance_account(self, _perm):
        with mock.patch.object(views.sap_service, "employee_advance_codes",
                               return_value={"JWPL0115", "JWPL2846"}) as codes:
            response = self._get("not_in_sap=1&company=MART")
        codes.assert_called_once_with("MART")
        self.assertEqual([r["employee_code"] for r in response.data["data"]["results"]],
                         ["JWPL0030", "JWPL3100"])

    def test_not_in_sap_needs_a_company_and_sap(self, _perm):
        self.assertEqual(self._get("not_in_sap=1").status_code, 400)
        with mock.patch.object(views.sap_service, "employee_advance_codes",
                               side_effect=views.sap_service.SapUnavailable("SAP down")):
            self.assertEqual(self._get("not_in_sap=1&company=OIL").status_code, 503)

    def test_refuses_an_unknown_role(self, _perm):
        self.assertEqual(self._get("roles=1,9").status_code, 400)


from datetime import date as _date  # noqa: E402

from advance_payment.services import invoice_fields  # noqa: E402

#: Real OCR text of an Oil bill attachment (RCM Logistics, SAP bill 51897).
RCM_BILL = [
    "M/S", "FREIGHT INVOICE", "RCM LOGISTICS", "ADDRESS :", "M/S", "JIVO WELLNESS PVT LIMITED",
    "BANK DETAILS :-", "Bank A/CNo.:777705145689", "BILL NO.", ":LDH005266",
    "IFSC CODE :ICICOO03662", "BILL DATE", "：24-08-2026", "20-08-2026", "13858", "5000",
    "SUB TOTAL:", "20358.00", "GRAND TOTAL:", "20358.00", "For RCM LOGISTICS", "Authorised Signatory",
]


class ReadsAnInvoice(SimpleTestCase):
    def test_reads_every_field_from_the_labels_alone(self):
        fields = invoice_fields.extract(RCM_BILL)
        self.assertEqual({k: v["value"] for k, v in fields.items()}, {
            "invoice_number": "LDH005266", "invoice_date": "2026-08-24", "amount": 20358.0,
            "party_name": "RCM LOGISTICS", "account_number": "777705145689",
            # OCR read both zeros as the letter O.
            "ifsc": "ICIC0003662",
        })

    def test_checks_each_field_against_sap(self):
        fields = invoice_fields.extract(RCM_BILL, {
            "invoice_number": "LDH005266", "invoice_date": _date(2026, 8, 24),
            # SAP's total is net of 1% TDS: DocTotal + WTSum is the invoice's figure.
            "totals": [20358.0, 20154.0], "party_name": "RCM LOGISTICS",
            "accounts": ["777705145689"], "ifscs": ["ICIC0003662"],
        })
        self.assertTrue(all(f["match"] for f in fields.values()), fields)

    def test_says_when_the_document_disagrees(self):
        fields = invoice_fields.extract(RCM_BILL, {
            "invoice_number": "LDH009999", "invoice_date": _date(2026, 8, 1),
            "totals": [99999.0], "accounts": ["11112222333"],
        })
        self.assertIs(fields["invoice_number"]["match"], False)
        self.assertIs(fields["invoice_date"]["match"], False)
        self.assertIs(fields["amount"]["match"], False)
        self.assertIs(fields["account_number"]["match"], False)

    def test_party_name_from_its_label_not_the_buyer(self):
        rows = ["Name   : KIRANAKART TECHNOLOGIES PVT LTD   Name   : Jivo Mart Private Limited",
                "Bank Name : HSBC"]
        self.assertEqual(invoice_fields.extract(rows)["party_name"]["value"], "KIRANAKART TECHNOLOGIES PVT LTD")
        rows = ["Invoice From :   Swiggy Limited (formerly known IRN :   0d8c", "as Swiggy Private Limited and"]
        self.assertEqual(invoice_fields.extract(rows, {"party_name": "SWIGGY LIMITED"})["party_name"],
                         {"value": "Swiggy Limited", "sap": "SWIGGY LIMITED", "match": True})

    def test_nothing_found_is_none_not_a_guess(self):
        fields = invoice_fields.extract(["Opening balance"], {"invoice_number": "X123"})
        self.assertEqual(fields["invoice_number"], {"value": None, "sap": "X123", "match": None})


@mock.patch.object(views.ap_perms.CanViewLookups, "has_permission", return_value=True)
@mock.patch.object(views.sap_service, "partner_bank_accounts",
                   return_value=([{"account_number": "777705145689", "ifsc": "ICIC0003662"}], ""))
@mock.patch.object(views.sap_service, "document_attachment",
                   return_value={"file_name": "bill.pdf", "count": 1, "date": "2026-09-17"})
@mock.patch.object(views.sap_service, "document_facts", return_value={
    "doc_num": 626093130, "vendor_ref": "LDH005266", "document_date": "2026-08-24",
    "doc_total": "20154.000000", "tds": "204.000000", "gross_total": "20358.000000",
    "card_code": "VENDA001", "card_name": "RCM LOGISTICS"})
class TheAttachmentReader(SimpleTestCase):
    def setUp(self):
        views.cache.clear()

    def _get(self, query="company=OIL&kind=bill&doc_entry=51897"):
        request = APIRequestFactory().get(f"/api/advance-payments/document-attachment/read/?{query}")
        force_authenticate(request, user=SimpleNamespace(pk=1, id=1, is_authenticated=True, is_active=True))
        return views.DocumentAttachmentReadView.as_view()(request)

    def test_reads_the_attachment_once_and_checks_it_against_sap(self, _facts, _meta, _banks, _perm):
        with mock.patch.object(views.attachment_files, "fetch", return_value=(b"%PDF", "application/pdf")) as fetch, \
                mock.patch.object(views.proof_reader, "read", return_value={
                    "source": "ocr", "rows": RCM_BILL, "pages": 1, "ocr_pages": 1}):
            first = self._get()
            second = self._get()
        self.assertEqual(first.status_code, 200)
        data = first.data["data"]
        self.assertEqual(data["source"], "ocr")
        self.assertTrue(all(f["match"] for f in data["fields"].values()), data["fields"])
        # The second look came from the cache: the file was fetched once.
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(second.data["data"]["fields"], data["fields"])

    def test_no_attachment_is_404_and_no_ocr_is_503(self, _facts, meta, _banks, _perm):
        meta.return_value = None
        self.assertEqual(self._get().status_code, 404)
        meta.return_value = {"file_name": "photo.jpg", "count": 1, "date": ""}
        with mock.patch.object(views.attachment_files, "fetch", return_value=(b"\xff\xd8", "image/jpeg")), \
                mock.patch.object(views.proof_reader, "read",
                                  side_effect=views.proof_reader.OcrUnavailable("needs OCR")):
            self.assertEqual(self._get().status_code, 503)


@mock.patch.object(views.ap_perms.CanViewLookups, "has_permission", return_value=True)
class TheDepartments(SimpleTestCase):
    def test_lists_active_departments_with_their_sub_departments(self, _perm):
        finance = SimpleNamespace(id=35, name="Finance")
        cyber = SimpleNamespace(id=40, name="Cyber Security")
        subs = [SimpleNamespace(id=92, name="AP", department_id=35),
                SimpleNamespace(id=88, name="AR", department_id=35)]
        request = APIRequestFactory().get("/api/advance-payments/departments/")
        force_authenticate(request, user=SimpleNamespace(pk=1, id=1, is_authenticated=True, is_active=True))
        with mock.patch.object(views.Department.objects, "filter") as departments, \
                mock.patch.object(views.SubDepartment.objects, "filter") as sub_departments:
            departments.return_value.order_by.return_value = [cyber, finance]
            sub_departments.return_value.order_by.return_value = subs
            response = views.DepartmentsView.as_view()(request)
        departments.assert_called_once_with(is_active=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["data"]["results"], [
            {"id": 40, "name": "Cyber Security", "sub_departments": []},
            {"id": 35, "name": "Finance", "sub_departments": [{"id": 92, "name": "AP"}, {"id": 88, "name": "AR"}]},
        ])
