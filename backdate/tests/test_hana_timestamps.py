"""Preserve arbitrary user-selected expiry instants at the SAP boundary."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, override_settings
from rest_framework import serializers

from backdate.serializers import BackDateCreateSerializer, ZonedExpiryField
from backdate.services.hana import HanaWriteError, build_payload, payload_json, sap_timestamp


@override_settings(HANA_TIME_ZONE='Asia/Kolkata', TIME_ZONE='UTC', USE_TZ=True)
class HanaTimestampTests(SimpleTestCase):
    def test_the_reported_1630_expiry_is_bound_as_1630(self):
        instant = ZonedExpiryField().run_validation('2026-10-14T16:30:00+05:30')
        self.assertEqual(instant, datetime(2026, 10, 14, 11, tzinfo=timezone.utc))
        self.assertEqual(sap_timestamp(instant), datetime(2026, 10, 14, 16, 30))
        self.assertEqual(instant.utcoffset().total_seconds(), 0)

    def test_midnight_preserves_the_sap_calendar_day_and_precision(self):
        instant = datetime(2026, 10, 13, 18, 35, 12, 345678, tzinfo=timezone.utc)
        self.assertEqual(sap_timestamp(instant), datetime(2026, 10, 14, 0, 5, 12, 345678))

    def test_naive_expiry_is_rejected_at_the_api_and_sap_boundary(self):
        with self.assertRaises(serializers.ValidationError):
            ZonedExpiryField().run_validation('2026-10-14T16:30:00')
        with self.assertRaises(HanaWriteError):
            sap_timestamp(datetime(2026, 10, 14, 16, 30))

    def test_payload_and_audit_record_the_values_actually_bound(self):
        request = SimpleNamespace(
            companies=['OIL'], sap_username='USER09', document_type_name='A/P Invoice',
            from_date=date(2026, 7, 1), to_date=date(2026, 7, 1),
            time_limit=datetime(2026, 10, 14, 11, tzinfo=timezone.utc),
            created_at=datetime(2026, 10, 14, 10, 13, 37, 55904, tzinfo=timezone.utc),
            created_by_id=121, action='A')
        with mock.patch('backdate.services.hana.sap_masters.object_type_for', return_value=18):
            params = build_payload(request)
        self.assertEqual(params[5], datetime(2026, 10, 14, 16, 30))
        self.assertEqual(params[8], datetime(2026, 10, 14, 15, 43, 37, 55904))
        self.assertIsNone(params[5].tzinfo)
        audit = payload_json('OIL', params)['parameters']
        self.assertEqual(audit['TIMELIMIT'], '2026-10-14T16:30:00')
        self.assertEqual(audit['CREATEDON'], '2026-10-14T15:43:37.055904')
        self.assertEqual(audit['FROMDATE'], '2026-07-01')
        self.assertEqual(audit['TODATE'], '2026-07-01')

    @override_settings(HANA_TIME_ZONE='UTC')
    def test_the_sap_timezone_is_configurable(self):
        instant = datetime(2026, 10, 14, 11, tzinfo=timezone.utc)
        self.assertEqual(sap_timestamp(instant), datetime(2026, 10, 14, 11))

    def test_expiry_validation_uses_the_sap_day_near_midnight(self):
        values = {
            'from_date': date(2026, 10, 14), 'to_date': date(2026, 10, 14),
            'time_limit': datetime(2026, 10, 13, 18, 35, tzinfo=timezone.utc)}
        with mock.patch('backdate.serializers.timezone.now',
                        return_value=datetime(2026, 10, 13, tzinfo=timezone.utc)):
            self.assertEqual(BackDateCreateSerializer().validate(values), values)

    def test_every_minute_of_the_day_reaches_sap_unchanged_in_ist(self):
        india = timezone(timedelta(hours=5, minutes=30))
        field = ZonedExpiryField()
        for hour in range(24):
            for minute in range(60):
                expected = datetime(2027, 1, 1, hour, minute, tzinfo=india)
                with self.subTest(hour=hour, minute=minute):
                    # Mobile and web submit the UTC representation of the pick.
                    submitted = expected.astimezone(timezone.utc).isoformat()
                    stored = field.run_validation(submitted)
                    self.assertEqual(sap_timestamp(stored), expected.replace(tzinfo=None))

    def test_calendar_boundaries_and_fractional_seconds_are_preserved(self):
        inputs = [
            '2026-01-01T00:00:00+05:30',
            '2026-10-14T09:15:01.123456+05:30',
            '2026-10-31T23:59:59.999999+05:30',
            '2026-12-31T23:59:59+05:30',
            '2027-01-01T00:00:01+05:30',
            '2028-02-29T00:01:59.000001+05:30',
            '2028-03-01T00:00:00+05:30',
        ]
        field = ZonedExpiryField()
        for value in inputs:
            with self.subTest(value=value):
                expected = datetime.fromisoformat(value)
                self.assertEqual(sap_timestamp(field.run_validation(value)),
                                 expected.replace(tzinfo=None))
                submitted = expected.astimezone(timezone.utc).isoformat()
                self.assertEqual(sap_timestamp(field.run_validation(submitted)),
                                 expected.replace(tzinfo=None))
