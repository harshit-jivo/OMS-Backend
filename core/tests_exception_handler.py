"""The API error envelope (plan item 3.8).

The property under test is COMPATIBILITY, not prettiness. The frontend reads
`error.response.data.message` in 15 places, `.detail` in 13 and `.error` in 11,
because endpoints answer with different keys. A handler that replaced those
would break every one of those call sites; this one fills in what is missing
and never overwrites what is there.
"""
import logging

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from django.test import TestCase
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response

from core.exception_handler import UNHANDLED_MESSAGE, api_exception_handler


class _View:
    """Stands in for the view DRF passes in `context`."""


def handle(exc):
    return api_exception_handler(exc, {'view': _View(), 'request': None})


class EnvelopeTests(TestCase):

    def test_the_three_keys_the_clients_read_are_always_present(self):
        for exc in (ValidationError('bad'), NotFound(), PermissionDenied()):
            with self.subTest(exc=type(exc).__name__):
                data = handle(exc).data
                for key in ('message', 'detail', 'error', 'success'):
                    self.assertIn(key, data)
                self.assertIs(data['success'], False)

    def test_field_errors_survive_untouched(self):
        """A serializer error is a dict of field -> [messages], and a client may
        read `data.card_code` directly to mark a form field. Replacing that
        structure with a flat message is the breaking change this avoids."""
        exc = ValidationError({'card_code': ['This field is required.']})
        data = handle(exc).data
        self.assertEqual(data['card_code'], ['This field is required.'])
        self.assertEqual(data['message'], 'card_code: This field is required.')

    def test_an_existing_key_is_never_overwritten(self):
        """`setdefault`, not assignment. An endpoint that already answers with
        its own wording keeps it."""
        exc = ValidationError({'detail': 'Order already approved.'})
        data = handle(exc).data
        self.assertEqual(data['detail'], 'Order already approved.')
        self.assertEqual(data['message'], 'Order already approved.')

    def test_a_non_field_error_does_not_invent_a_field_name(self):
        exc = ValidationError({'non_field_errors': ['Dates overlap.']})
        self.assertEqual(handle(exc).data['message'], 'Dates overlap.')

    def test_a_list_body_is_kept_under_errors(self):
        data = handle(ValidationError(['first problem', 'second'])).data
        self.assertEqual(data['errors'], ['first problem', 'second'])
        self.assertEqual(data['message'], 'first problem')


class UnhandledExceptionTests(TestCase):
    """Before this handler, an unhandled exception left DRF with nothing to
    render and the API answered with Django's HTML 500 page — HTML, to a JSON
    client."""

    def test_an_unhandled_exception_becomes_json_500(self):
        logging.disable(logging.CRITICAL)
        try:
            response = handle(RuntimeError('boom'))
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(response.status_code,
                         status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertIs(response.data['success'], False)

    def test_the_exception_text_is_not_returned_to_the_caller(self):
        """The message of an unexpected exception routinely carries a query, a
        path or a row of data. None of that belongs in an API response."""
        logging.disable(logging.CRITICAL)
        try:
            data = handle(RuntimeError(
                'FATAL: password authentication failed for user "oms"')).data
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(data['message'], UNHANDLED_MESSAGE)
        self.assertNotIn('password', str(data))

    def test_it_is_logged_with_a_traceback(self):
        """Hiding the detail from the caller is only acceptable because it is
        kept for the operator.

        The exception is RAISED rather than merely constructed. A
        never-raised exception has no `__traceback__`, so `exc_info` renders
        only the exception line — asserting against a bare
        `RuntimeError('boom')` would be testing something DRF never does.
        """
        try:
            raise RuntimeError('boom')
        except RuntimeError as exc:
            with self.assertLogs('core.exception_handler', level='ERROR') as caught:
                handle(exc)
        joined = '\n'.join(caught.output)
        self.assertIn('boom', joined)
        self.assertIn('Traceback', joined)
        self.assertIn('tests_exception_handler', joined,
                      'the traceback should name where it was raised')


class DjangoExceptionTests(TestCase):
    """Django's own exceptions reach DRF unconverted; without this they were
    the HTML-500 case too."""

    def test_django_http404_becomes_a_json_404(self):
        """DRF's own handler converts Http404, carrying the message through —
        so a raised `Http404('no such order')` reaches the client as that
        wording. Worth pinning: it means the text of an Http404 IS
        client-visible, unlike an unhandled exception's."""
        response = handle(Http404('no such order'))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(str(response.data['message']), 'no such order')
        self.assertIs(response.data['success'], False)

    def test_django_permission_denied_becomes_a_json_403(self):
        response = handle(DjangoPermissionDenied())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIs(response.data['success'], False)

    def test_the_status_code_is_taken_from_the_exception(self):
        self.assertEqual(handle(NotFound()).status_code, 404)
        self.assertEqual(handle(PermissionDenied()).status_code, 403)
        self.assertEqual(handle(ValidationError('x')).status_code, 400)


class WiringTests(TestCase):
    """The tests above call the handler directly, which proves it works and
    NOT that DRF uses it. `EXCEPTION_HANDLER` is a settings string: a typo
    there leaves every test above passing and the API unchanged."""

    def test_the_setting_points_at_this_handler(self):
        from rest_framework.settings import api_settings

        self.assertIs(api_settings.EXCEPTION_HANDLER, api_exception_handler)

    def test_a_real_request_gets_the_envelope(self):
        """End to end through DRF's dispatch: an anonymous request to a closed
        endpoint is refused by DEFAULT_PERMISSION_CLASSES, and the refusal
        comes back carrying all three keys the clients read."""
        from rest_framework.test import APIClient

        response = APIClient().get('/api/schema/')
        self.assertIn(response.status_code, (401, 403))
        for key in ('message', 'detail', 'error', 'success'):
            self.assertIn(key, response.data, f'{key} missing from {response.data}')
        self.assertIs(response.data['success'], False)
