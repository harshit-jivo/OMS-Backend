"""The day's cut-off (user, 2026-10-21): after 4:30 pm IST nobody but Audit may
act on a payment request; after 6:30 pm, Audit neither. Until midnight."""
from datetime import datetime
from unittest import mock

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from advance_payment import views
from advance_payment.services import clock


def at(hour, minute):
    """A moment on 22 Oct 2026, the first day of the cut-off."""
    return datetime(2026, 10, 22, hour, minute, tzinfo=clock.INDIA)


class TheCutOff(SimpleTestCase):
    def test_everyone_until_four_thirty_audit_until_six_thirty(self):
        self.assertEqual(clock.closed_for(False, at(16, 29)), '')
        self.assertIn('after 04:30 PM (IST): only Audit may act until 06:30 PM', clock.closed_for(False, at(16, 30)))
        self.assertEqual(clock.closed_for(True, at(18, 29)), '')
        self.assertIn('after 06:30 PM (IST), Audit included', clock.closed_for(True, at(18, 30)))
        self.assertIn('Audit included', clock.closed_for(False, at(23, 59)))
        # A new day: open again.
        self.assertEqual(clock.closed_for(False, datetime(2026, 10, 23, 0, 0, tzinfo=clock.INDIA)), '')
        self.assertEqual(clock.closed_for(False, at(9, 0)), '')

    def test_on_indias_clock_whatever_the_servers(self):
        from datetime import timezone as tz
        utc = datetime(2026, 10, 22, 11, 5, tzinfo=tz.utc)  # 4:35 pm IST
        self.assertIn('04:30 PM', clock.closed_for(False, utc))

    def test_not_before_its_first_day(self):
        # "For now remove the time lock … from tomorrow" (2026-10-21).
        self.assertEqual(clock.closed_for(False, datetime(2026, 10, 21, 18, 45, tzinfo=clock.INDIA)), '')
        with self.settings(ADVANCE_PAYMENT_CUTOFF_FROM='2026-11-01'):
            self.assertEqual(clock.closed_for(False, at(19, 0)), '')

    def test_the_times_are_settings(self):
        with self.settings(ADVANCE_PAYMENT_CUTOFF='17:00', ADVANCE_PAYMENT_AUDIT_CUTOFF='19:00'):
            self.assertEqual(clock.closed_for(False, at(16, 45)), '')
            self.assertIn('05:00 PM', clock.closed_for(False, at(17, 0)))


class EveryChangeIsRefusedAfterIt(SimpleTestCase):
    def call(self, method, at_audit, when, **kwargs):
        factory = APIRequestFactory()
        request = getattr(factory, method)('/x/', {}, format='json')
        force_authenticate(request, user=mock.Mock(is_authenticated=True, pk=1))
        view = views.RequestActionView.as_view()
        with mock.patch.object(clock, 'now', return_value=when), \
                mock.patch.object(views._RequestView, '_at_audit', return_value=at_audit), \
                mock.patch.object(views.RequestActionView, 'post', return_value=views.ok({})) as post:
            response = view(request, pk=7, action='approve', **kwargs)
        return response, post

    def test_after_four_thirty_only_a_request_at_audit_is_acted_on(self):
        response, post = self.call('post', False, at(16, 31))
        self.assertEqual(response.status_code, 423)
        self.assertIn('only Audit may act', str(response.data))
        post.assert_not_called()
        response, post = self.call('post', True, at(16, 31))
        self.assertEqual(response.status_code, 200)
        post.assert_called_once()

    def test_after_six_thirty_audit_neither(self):
        response, post = self.call('post', True, at(18, 31))
        self.assertEqual(response.status_code, 423)
        post.assert_not_called()

    def test_before_four_thirty_everything_goes(self):
        response, post = self.call('post', False, at(11, 0))
        self.assertEqual(response.status_code, 200)
