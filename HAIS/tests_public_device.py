"""The public device page behind a scanned QR sticker.

The sticker is read by whatever phone is to hand, usually by someone with no
OMS account — so this endpoint has no session behind it, and the field list is
the whole security control. These tests are that control, written down.
"""
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .models import Asset, AssetType, Department
from .serializers import PublicAssetSerializer


class PublicDeviceEndpointTests(TestCase):
    def setUp(self):
        self.laptop_type = AssetType.objects.create(name="Laptop", id_prefix="LAP")
        self.it = Department.objects.create(name="IT")
        self.asset = Asset.objects.create(
            asset_id="JIVO-LAP-0001",
            serial_num="5CD1234XYZ",
            asset_type=self.laptop_type,
            company="Dell",
            model_num="Latitude 5420",
            working_status="Working",
            processor="i5-1145G7",
            memory="16 GB",
            operating_system="Windows 11",
            storage="512 GB",
            warranty_ends="2027-03-01",
            current_user_name="Priya Sharma",
            current_user_id="EMP-2201",
            prev_user_name="Rahul Verma",
            prev_user_id="EMP-1180",
            department=self.it,
            email_id="priya.sharma@jivo.in",
            current_location="Gurugram HO",
            purchase_invoice_no="INV-99812",
            amount="72500.00",
            vendor="Redington",
        )
        self.url = reverse("hais-public-device")

    def _get(self, code):
        return self.client.get(self.url, {"code": code})

    # ── It works without a session ─────────────────────────────────────────

    def test_an_anonymous_scan_resolves_the_device(self):
        # The whole point: no login, because the person holding the phone has
        # no account and never will.
        res = self._get("5CD1234XYZ")

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["asset_id"], "JIVO-LAP-0001")

    def test_the_serial_match_is_case_insensitive(self):
        # Serials are read off a sticker by eye, or decoded by a scanner that
        # may not preserve case.
        self.assertEqual(self._get("5cd1234xyz").status_code, 200)

    def test_it_shows_the_device_and_how_to_return_it(self):
        body = self._get("5CD1234XYZ").json()

        self.assertEqual(body["model_num"], "Latitude 5420")
        self.assertEqual(body["working_status"], "Working")
        self.assertEqual(body["asset_type_name"], "Laptop")
        # Who has it, and a way to reach them — the reason a finder scans.
        self.assertEqual(body["current_user_name"], "Priya Sharma")
        self.assertEqual(body["email_id"], "priya.sharma@jivo.in")
        self.assertEqual(body["department_name"], "IT")

    # ── What it must never show ────────────────────────────────────────────

    def test_it_does_not_leak_employee_numbers_or_the_previous_holder(self):
        # A sticker is readable by anyone holding the machine. The current
        # holder is the point; the person before them is nobody's business,
        # and an employee number is an internal identifier.
        body = self._get("5CD1234XYZ").json()

        for field in ("current_user_id", "prev_user_name", "prev_user_id"):
            self.assertNotIn(field, body, f"{field} must not be public")

    def test_it_does_not_leak_what_the_company_paid(self):
        body = self._get("5CD1234XYZ").json()

        for field in ("purchase_invoice_no", "purchase_invoice_date", "amount", "vendor"):
            self.assertNotIn(field, body, f"{field} must not be public")

    def test_it_does_not_leak_the_handover_history(self):
        # `logs` is a movement record of named staff — who held this machine,
        # when, and why it moved.
        self.assertNotIn("logs", self._get("5CD1234XYZ").json())

    def test_the_field_list_is_an_allow_list(self):
        # The real guard: adding a column to Asset, or a field to
        # AssetSerializer, must never widen this response. If this fails, a
        # field was added to PublicAssetSerializer — decide deliberately
        # whether an anonymous scanner may see it.
        self.assertEqual(
            set(self._get("5CD1234XYZ").json()),
            {
                "asset_id", "serial_num", "asset_type_name", "company", "model_num",
                "working_status", "processor", "memory", "operating_system", "storage",
                "storage_type_names", "warranty_ends", "current_user_name",
                "department_name", "email_id", "current_location",
            },
        )

    # ── It cannot be walked ────────────────────────────────────────────────

    def test_a_sequential_asset_id_does_NOT_resolve(self):
        # THE enumeration guard. `_next_asset_id` hands out JIVO-LAP-0001,
        # 0002, 0003 — so if this path accepted an Asset ID, anyone could walk
        # the range and collect every staff name and email in the register.
        # A serial number is a manufacturer string and cannot be guessed.
        res = self._get("JIVO-LAP-0001")

        self.assertEqual(res.status_code, 404)

    def test_an_unknown_code_says_the_same_thing_as_a_malformed_one(self):
        # Confirming that a serial EXISTS is itself a small leak.
        self.assertEqual(self._get("NOPE-123").status_code, 404)
        self.assertEqual(self._get("../../etc/passwd").status_code, 404)

    def test_a_missing_code_is_a_bad_request_not_a_dump(self):
        res = self.client.get(self.url)

        self.assertEqual(res.status_code, 400)


class PublicAssetSerializerFieldsTests(SimpleTestCase):
    """The allow-list, without a database.

    The field list IS the security control on an endpoint that anyone can
    reach, so it is worth pinning somewhere that runs anywhere — the tests
    above need a database, and the machine this was written on cannot create
    one. Serializing an unsaved instance exercises the same declaration.
    """

    EXPECTED = {
        "asset_id", "serial_num", "asset_type_name", "company", "model_num",
        "working_status", "processor", "memory", "operating_system", "storage",
        "storage_type_names", "warranty_ends", "current_user_name",
        "department_name", "email_id", "current_location",
    }

    def test_the_public_shape_is_exactly_this(self):
        # Adding a field to Asset or to AssetSerializer must not widen it. If
        # this fails, somebody added a field here — decide deliberately whether
        # an anonymous scanner may see it.
        self.assertEqual(set(PublicAssetSerializer().fields), self.EXPECTED)

    def test_it_withholds_the_things_a_sticker_must_not_publish(self):
        fields = set(PublicAssetSerializer().fields)

        for field in (
            "current_user_id",      # employee number
            "prev_user_id",
            "prev_user_name",       # the previous holder
            "purchase_invoice_no",  # what it cost, and from whom
            "purchase_invoice_date",
            "amount",
            "vendor",
            "logs",                 # the handover history of named staff
            "remarks",              # free text, written for internal readers
        ):
            self.assertNotIn(field, fields, f"{field} must never be public")

    def test_every_public_field_is_read_only(self):
        # It is a GET-only view, but a serializer that accepts writes is one
        # refactor away from being reused on a POST.
        for name, field in PublicAssetSerializer().fields.items():
            self.assertTrue(field.read_only, f"{name} should be read-only")
