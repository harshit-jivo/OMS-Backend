"""The day's cut-off (user, 2026-10-22): after 4:30 pm IST no new payment request
is raised; after 6:30 pm no desk acts either. Until midnight; from Monday 26 Oct."""
from datetime import datetime
from unittest import mock

from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from advance_payment import views
from advance_payment.services import clock


def at(hour, minute):
    """A moment on 26 Oct 2026, the first day of the cut-off."""
    return datetime(2026, 10, 26, hour, minute, tzinfo=clock.INDIA)


class TheCutOff(SimpleTestCase):
    def test_raising_until_four_thirty_the_desks_until_six_thirty(self):
        self.assertEqual(clock.closed_for(True, at(16, 29)), '')
        self.assertIn('New payment requests are closed', clock.closed_for(True, at(16, 30)))
        self.assertEqual(clock.closed_for(False, at(16, 30)), '')
        self.assertEqual(clock.closed_for(False, at(18, 29)), '')
        self.assertIn('after 06:30 PM (IST)', clock.closed_for(False, at(18, 30)))
        self.assertIn('after 06:30 PM (IST)', clock.closed_for(True, at(23, 59)))
        # A new day: open again.
        self.assertEqual(clock.closed_for(True, datetime(2026, 10, 27, 0, 0, tzinfo=clock.INDIA)), '')

    def test_on_indias_clock_whatever_the_servers(self):
        from datetime import timezone as tz
        utc = datetime(2026, 10, 26, 11, 5, tzinfo=tz.utc)  # 4:35 pm IST
        self.assertIn('04:30 PM', clock.closed_for(True, utc))

    def test_not_before_monday(self):
        # "Apply lock from monday onwards, for now remove the locks" (2026-10-22).
        for day in (22, 23, 24, 25):
            self.assertEqual(clock.closed_for(True, datetime(2026, 10, day, 19, 0, tzinfo=clock.INDIA)), '')
        with self.settings(ADVANCE_PAYMENT_CUTOFF_FROM='2026-11-01'):
            self.assertEqual(clock.closed_for(True, at(19, 0)), '')

    def test_the_times_are_settings(self):
        with self.settings(ADVANCE_PAYMENT_CUTOFF='17:00', ADVANCE_PAYMENT_AUDIT_CUTOFF='19:00'):
            self.assertEqual(clock.closed_for(True, at(16, 45)), '')
            self.assertIn('05:00 PM', clock.closed_for(True, at(17, 0)))


class TheViewsRefuseIt(SimpleTestCase):
    def call(self, view_class, when, **kwargs):
        factory = APIRequestFactory()
        request = factory.post('/x/', {}, format='json')
        force_authenticate(request, user=mock.Mock(is_authenticated=True, pk=1))
        with mock.patch.object(clock, 'now', return_value=when), \
                mock.patch.object(view_class, 'post', return_value=views.ok({})) as post:
            response = view_class.as_view()(request, **kwargs)
        return response, post

    def test_after_four_thirty_no_request_is_raised_but_the_desks_act(self):
        response, post = self.call(views.RequestListView, at(16, 31))
        self.assertEqual(response.status_code, 423)
        post.assert_not_called()
        response, post = self.call(views.RequestActionView, at(16, 31), pk=7, action='approve')
        self.assertEqual(response.status_code, 200)

    def test_after_six_thirty_nothing_moves(self):
        response, post = self.call(views.RequestActionView, at(18, 31), pk=7, action='approve')
        self.assertEqual(response.status_code, 423)
        post.assert_not_called()

    def test_before_four_thirty_everything_goes(self):
        response, _post = self.call(views.RequestListView, at(11, 0))
        self.assertEqual(response.status_code, 200)
