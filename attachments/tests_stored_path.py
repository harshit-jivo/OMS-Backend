"""The stored path records where a file really went — and is never trusted.

`stored_path` is the attachment's identity: it is the only record of where the
bytes actually landed, and recomputing a location from today's settings answers
"where would this go now", which stops matching reality the moment a share is
repointed.

That makes it data out of the database being handed to open(), so every test
here that is not about correctness is about refusing to become an arbitrary
file-read primitive.
"""
import os

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from .storage import _basename, directory_for, resolve_stored_path

ROOT = os.path.join(os.sep, 'srv', 'shares', 'payments')
DEPOSIT_ROOT = os.path.join(os.sep, 'srv', 'shares', 'deposits')


@override_settings(PAYMENTS_IMAGES=ROOT, DEPOSIT_PAYMENTS_IMAGES=DEPOSIT_ROOT,
                   PAYMENTS_SMB_USERNAME='')
class ResolveStoredPathTests(TestCase):
    """A stored path is used only when it proves it is one of ours."""

    def test_a_valid_stored_path_is_used_verbatim(self):
        stored = os.path.join(ROOT, 'abc123.jpg')
        path, _ = resolve_stored_path(stored, 'CHEQUE_IMAGE')
        self.assertEqual(path, stored)

    def test_the_deposit_root_is_also_accepted(self):
        """Either configured share is legitimate, not just the caller's own."""
        stored = os.path.join(DEPOSIT_ROOT, 'slip.pdf')
        path, _ = resolve_stored_path(stored, 'DEPOSIT_SLIP')
        self.assertEqual(path, stored)

    def test_a_subdirectory_inside_the_root_is_accepted(self):
        stored = os.path.join(ROOT, '2026', 'may.jpg')
        path, _ = resolve_stored_path(stored, 'CHEQUE_IMAGE')
        self.assertEqual(path, stored)

    def test_an_empty_path_resolves_into_the_configured_share(self):
        """Cannot happen now the column is NOT NULL and unique, but the
        resolver must still not hand back something outside the share."""
        path, _ = resolve_stored_path('', 'CHEQUE_IMAGE')
        self.assertTrue(path.startswith(ROOT))


@override_settings(PAYMENTS_IMAGES=ROOT, DEPOSIT_PAYMENTS_IMAGES=DEPOSIT_ROOT,
                   PAYMENTS_SMB_USERNAME='')
class RejectedPathTests(TestCase):
    """Every rejection falls back to the safe path rather than raising.

    Falling back, not failing, is deliberate: a bad row should still serve its
    file from the configured share if it is there. The refusal is about never
    READING somewhere we were told to by the database.
    """

    def test_a_path_outside_every_root_is_refused(self):
        stored = os.path.join(os.sep, 'etc', 'abc123.jpg')
        path, _ = resolve_stored_path(stored, 'CHEQUE_IMAGE')
        self.assertEqual(path, os.path.join(ROOT, 'abc123.jpg'))

    def test_a_traversal_path_is_refused(self):
        stored = os.path.join(ROOT, '..', '..', 'etc', 'abc123.jpg')
        path, _ = resolve_stored_path(stored, 'CHEQUE_IMAGE')
        self.assertEqual(path, os.path.join(ROOT, 'abc123.jpg'))

    def test_a_sibling_root_prefix_is_refused(self):
        """'/srv/shares/payments_evil' must not pass as '/srv/shares/payments'."""
        evil = ROOT + '_evil'
        stored = os.path.join(evil, 'abc123.jpg')
        path, _ = resolve_stored_path(stored, 'CHEQUE_IMAGE')
        self.assertEqual(path, os.path.join(ROOT, 'abc123.jpg'))

    def test_an_absolute_path_to_a_system_file_is_refused(self):
        stored = os.path.join(os.sep, 'etc', 'passwd')
        path, _ = resolve_stored_path(stored, 'CHEQUE_IMAGE')
        self.assertEqual(path, os.path.join(ROOT, 'passwd'))

    def test_a_refused_path_never_escapes_the_share(self):
        """The property that actually matters, stated directly."""
        for stored in (os.path.join(os.sep, 'etc', 'shadow'),
                       os.path.join(ROOT, '..', 'secrets', 'x.jpg'),
                       os.path.join(ROOT + '_evil', 'x.jpg')):
            path, _ = resolve_stored_path(stored, 'CHEQUE_IMAGE')
            self.assertTrue(path.startswith(ROOT),
                            f'{stored!r} resolved outside the share: {path!r}')


class BasenameTests(TestCase):
    """Separator handling, which is where cross-platform path checks break."""

    def test_a_windows_path_splits_on_backslashes(self):
        """os.path.basename does NOT do this on POSIX — hence the helper."""
        self.assertEqual(
            _basename(r'C:\Users\Jivo\images\Receive_Payments\96e2.jpeg'),
            '96e2.jpeg')

    def test_a_unc_share_path_splits_correctly(self):
        self.assertEqual(
            _basename(r'\\JIVO-APP\Payments\Receive_Payments\96e2.jpeg'),
            '96e2.jpeg')

    def test_a_posix_path_splits_correctly(self):
        self.assertEqual(_basename('/srv/shares/payments/96e2.jpeg'),
                         '96e2.jpeg')


@override_settings(PAYMENTS_IMAGES=r'\\JIVO-APP\Payments\Receive_Payments',
                   DEPOSIT_PAYMENTS_IMAGES=r'\\JIVO-APP\Payments\Deposits',
                   PAYMENTS_SMB_USERNAME='')
class WindowsSharePathTests(TestCase):
    """The real deployment stores Windows/UNC paths."""

    def test_a_unc_stored_path_is_accepted(self):
        stored = r'\\JIVO-APP\Payments\Receive_Payments\96e2dbc2b49c.jpeg'
        path, _ = resolve_stored_path(stored, 'CHEQUE_IMAGE')
        self.assertEqual(_basename(path), '96e2dbc2b49c.jpeg')

    def test_a_unc_path_on_a_different_server_is_refused(self):
        stored = r'\\ATTACKER\Payments\Receive_Payments\96e2dbc2b49c.jpeg'
        path, _ = resolve_stored_path(stored, 'CHEQUE_IMAGE')
        self.assertNotIn('ATTACKER', path)


class UnconfiguredShareTests(TestCase):
    """A missing setting must fail loudly, not resolve to a bare filename."""

    @override_settings(PAYMENTS_IMAGES='', DEPOSIT_PAYMENTS_IMAGES='')
    def test_directory_for_raises_when_unconfigured(self):
        with self.assertRaises(ValidationError):
            directory_for('CHEQUE_IMAGE')


class StoredNameIsDerivedTests(TestCase):
    """`stored_name` is the path's basename, not a column."""

    def test_the_property_returns_the_basename(self):
        from .models import Attachment
        a = Attachment(stored_path=r'C:\share\Receive_Payments\abc123.jpeg')
        self.assertEqual(a.stored_name, 'abc123.jpeg')

    def test_it_handles_a_posix_path(self):
        from .models import Attachment
        a = Attachment(stored_path='/srv/shares/payments/abc123.jpeg')
        self.assertEqual(a.stored_name, 'abc123.jpeg')

    def test_it_handles_a_unc_path(self):
        from .models import Attachment
        a = Attachment(stored_path=r'\\JIVO-APP\Payments\x\abc123.jpeg')
        self.assertEqual(a.stored_name, 'abc123.jpeg')


class SerializerExposureTests(TestCase):
    """The path and the filename are infrastructure; neither reaches a client."""

    def test_no_path_or_filename_field_is_exposed(self):
        from .serializers import AttachmentSerializer
        fields = AttachmentSerializer().fields
        for leaked in ('stored_path', 'stored_name', 'original_name'):
            self.assertNotIn(leaked, fields)

    def test_the_file_extension_is_exposed_instead(self):
        """Enough for a client to pick an icon, with no name or path."""
        from .serializers import AttachmentSerializer
        self.assertIn('file_type', AttachmentSerializer().fields)
