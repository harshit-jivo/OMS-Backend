"""The storage-path check, and the paste it exists to catch.

`PAYMENTS_IMAGES` was once set to a PowerShell prompt plus the path —
`PS C:\\Users\\...`. Nothing complained until someone photographed a cheque, at
which point `os.makedirs` tried to create a directory named `PS C:`, Windows
returned WinError 123, and the upload became a 502 that the mobile client
swallowed into a note appended to the success message. Four uploads were lost
that way before anyone noticed.
"""
from django.test import SimpleTestCase, override_settings

from .checks import check_attachment_storage, looks_absolute

GOOD = r'C:\Users\JIVO\Desktop\PROJECT\OMS-Backend\media\PaymentAttachments\Recieve'
UNC = r'\\JIVO-APP\Payments\Receive_Payments'
PROMPTED = 'PS ' + GOOD


def ids(problems):
    return sorted(p.id for p in problems)


class LooksAbsoluteTests(SimpleTestCase):
    """One predicate carries the whole check — see the module docstring."""

    def test_a_windows_drive_path_is_accepted(self):
        self.assertTrue(looks_absolute(GOOD))

    def test_a_unc_share_is_accepted(self):
        """Production stores on a UNC share, so this must pass even when the
        server itself runs on Linux — which is why the check never uses
        `os.path.isabs`."""
        self.assertTrue(looks_absolute(UNC))

    def test_a_posix_path_is_accepted(self):
        self.assertTrue(looks_absolute('/srv/payments/receive'))

    def test_THE_POWERSHELL_PROMPT_IS_REJECTED(self):
        self.assertFalse(looks_absolute(PROMPTED))

    def test_a_quoted_path_is_rejected(self):
        self.assertFalse(looks_absolute('"' + GOOD + '"'))

    def test_a_relative_path_is_rejected(self):
        self.assertFalse(looks_absolute(r'media\PaymentAttachments'))


class CheckTests(SimpleTestCase):
    @override_settings(PAYMENTS_IMAGES=GOOD, DEPOSIT_PAYMENTS_IMAGES=UNC)
    def test_a_correct_configuration_is_silent(self):
        self.assertEqual(check_attachment_storage(None), [])

    @override_settings(PAYMENTS_IMAGES=PROMPTED, DEPOSIT_PAYMENTS_IMAGES=GOOD)
    def test_the_powershell_prompt_is_an_ERROR_not_a_warning(self):
        """An ERROR refuses to start, and this must not be shruggable: the
        symptom is silently lost attachments."""
        problems = check_attachment_storage(None)
        self.assertEqual(ids(problems), ['attachments.E002'])
        self.assertIn('PAYMENTS_IMAGES', problems[0].msg)
        # The value is quoted into the message because the defect is invisible
        # when the line is read casually.
        self.assertIn('PS ', problems[0].msg)

    @override_settings(PAYMENTS_IMAGES='  ' + GOOD + '  ',
                       DEPOSIT_PAYMENTS_IMAGES=GOOD)
    def test_padding_is_reported_as_whitespace_not_as_not_absolute(self):
        """The user needs to know WHICH edit to make."""
        self.assertEqual(ids(check_attachment_storage(None)),
                         ['attachments.E001'])

    @override_settings(PAYMENTS_IMAGES='', DEPOSIT_PAYMENTS_IMAGES=GOOD)
    def test_an_unset_path_is_only_a_WARNING(self):
        """A deployment that never receives attachments is legitimate, and
        refusing to boot over it would be worse than the upload failing."""
        self.assertEqual(ids(check_attachment_storage(None)),
                         ['attachments.W001'])

    @override_settings(PAYMENTS_IMAGES=None, DEPOSIT_PAYMENTS_IMAGES='   ')
    def test_none_and_blank_are_both_treated_as_unset(self):
        self.assertEqual(ids(check_attachment_storage(None)),
                         ['attachments.W001', 'attachments.W001'])

    @override_settings(PAYMENTS_IMAGES=PROMPTED,
                       DEPOSIT_PAYMENTS_IMAGES=r'relative\path')
    def test_both_settings_are_reported_independently(self):
        """One bad path must not mask the other."""
        self.assertEqual(ids(check_attachment_storage(None)),
                         ['attachments.E002', 'attachments.E002'])
