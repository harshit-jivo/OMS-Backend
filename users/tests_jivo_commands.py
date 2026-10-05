"""Tests for the Jivo Auth migration commands.

`export_jivo_users` writes OMS users for Jivo Auth's `import_users`,
`link_jivo_users` stores the Jivo IDs it hands back, and `sync_jivo_users`
refreshes the local copy of Jivo-owned fields. See
docs/jivo-auth-integration.md.

These are the commands that touch the user table during the migration, so
the refusals matter as much as the happy path: an export that leaks hashes
into the repository, or a link that attaches a Jivo ID to the wrong person,
is the failure being guarded against.

No test calls auth.jivo.in: `AuthClient` is mocked.

Run with::

    python manage.py test users.tests_jivo_commands --settings=OMS.test_settings
"""
import json
import os
import stat
import tempfile
import uuid
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from users.jivo import full_name
from users.models import User


def _user(username, email, **kwargs):
    return User.objects.create_user(
        username=username, password='CorrectHorse9!', name=kwargs.pop('name', username),
        email=email, **kwargs)


class _TempDirTestCase(TestCase):
    """A scratch directory outside BASE_DIR, where the export may write."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def run_command(self, *args, **kwargs):
        """(stderr) of the command; the commands report on stderr."""
        err = StringIO()
        call_command(*args, stdout=StringIO(), stderr=err, **kwargs)
        return err.getvalue()


class ExportJivoUsersTests(_TempDirTestCase):

    def export(self, *args):
        path = self.tmp / 'users.json'
        report = self.run_command('export_jivo_users', str(path), *args)
        return json.loads(path.read_text()), report, path

    def test_exports_active_unlinked_users_with_name_as_first_name(self):
        user = _user('priya', 'Priya.Sharma@jivo.in', name='Priya Sharma')

        rows, _, _ = self.export()

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['source_id'], user.pk)
        self.assertEqual(row['email'], 'Priya.Sharma@jivo.in')
        # OMS has one `name`; all of it goes to Jivo as first_name.
        self.assertEqual(row['first_name'], 'Priya Sharma')
        self.assertNotIn('last_name', row)
        # The stored hash verbatim (MD5 under test_settings, PBKDF2 for real).
        self.assertEqual(row['password'], user.password)
        self.assertIs(row['is_active'], True)

    def test_skips_users_without_email_and_reports_them(self):
        _user('with-email', 'a@jivo.in')
        no_email = _user('no-email', None)
        blank_email = _user('blank-email', '   ')

        rows, report, _ = self.export()

        self.assertEqual([row['email'] for row in rows], ['a@jivo.in'])
        self.assertIn(f'no email: pk={no_email.pk} no-email', report)
        self.assertIn(f'no email: pk={blank_email.pk} blank-email', report)

    def test_never_sends_staff_flags(self):
        _user('boss', 'boss@jivo.in', is_staff=True, is_superuser=True)

        rows, _, _ = self.export()

        for flag in ('is_staff', 'is_superuser', 'groups', 'user_permissions', 'role'):
            self.assertNotIn(flag, rows[0])

    def test_leaves_out_linked_and_inactive_users(self):
        _user('linked', 'linked@jivo.in', auth_id=uuid.uuid4())
        _user('gone', 'gone@jivo.in', is_active=False)
        _user('todo', 'todo@jivo.in')

        rows, report, _ = self.export()

        self.assertEqual([row['email'] for row in rows], ['todo@jivo.in'])
        self.assertIn('1 inactive (not exported)', report)

    def test_include_inactive_exports_them_as_inactive(self):
        _user('gone', 'gone@jivo.in', is_active=False)

        rows, _, _ = self.export('--include-inactive')

        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0]['is_active'], False)

    def test_file_is_owner_only(self):
        _user('priya', 'priya@jivo.in')

        _, _, path = self.export()

        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_refuses_to_write_inside_the_project(self):
        target = Path(settings.BASE_DIR) / 'users.json'

        with self.assertRaisesMessage(CommandError, 'outside the project'):
            self.run_command('export_jivo_users', str(target))

        self.assertFalse(target.exists())

    def test_refuses_to_overwrite(self):
        path = self.tmp / 'users.json'
        path.write_text('keep me')

        with self.assertRaisesMessage(CommandError, 'exists'):
            self.run_command('export_jivo_users', str(path))

        self.assertEqual(path.read_text(), 'keep me')


class LinkJivoUsersTests(_TempDirTestCase):

    def mapping(self, *rows, **extra):
        path = self.tmp / f'mapping-{uuid.uuid4().hex}.json'
        path.write_text(json.dumps({'summary': {}, 'users': list(rows), **extra}))
        return str(path)

    @staticmethod
    def row(user, auth_id, status='created', email=None):
        return {
            'source_id': user.pk,
            'email': (email or user.email).lower(),
            'auth_id': str(auth_id),
            'status': status,
            'notes': [],
        }

    def test_sets_auth_id_and_nothing_else(self):
        user = _user('priya', 'Priya.Sharma@jivo.in', name='Priya Sharma')
        before = User.objects.filter(pk=user.pk).values().get()
        auth_id = uuid.uuid4()

        self.run_command('link_jivo_users', self.mapping(self.row(user, auth_id)))

        after = User.objects.filter(pk=user.pk).values().get()
        self.assertEqual(after.pop('auth_id'), auth_id)
        before.pop('auth_id')
        self.assertEqual(after, before)

    def test_links_rows_import_linked_to_existing_accounts(self):
        user = _user('naresh', 'naresh@jivo.in')
        auth_id = uuid.uuid4()

        self.run_command('link_jivo_users', self.mapping(self.row(user, auth_id, status='linked')))

        user.refresh_from_db()
        self.assertEqual(user.auth_id, auth_id)

    def test_refuses_when_the_local_email_changed_since_the_export(self):
        user = _user('priya', 'priya@jivo.in')
        row = self.row(user, uuid.uuid4(), email='someone.else@jivo.in')

        report = self.run_command('link_jivo_users', self.mapping(row))

        user.refresh_from_db()
        self.assertIsNone(user.auth_id)
        self.assertIn('conflict', report)
        self.assertIn('local email is now', report)

    def test_refuses_an_auth_id_another_user_already_has(self):
        auth_id = uuid.uuid4()
        holder = _user('holder', 'holder@jivo.in', auth_id=auth_id)
        other = _user('other', 'other@jivo.in')

        report = self.run_command('link_jivo_users', self.mapping(self.row(other, auth_id)))

        other.refresh_from_db()
        self.assertIsNone(other.auth_id)
        self.assertIn(f'auth_id already used by local user pk={holder.pk}', report)

    def test_refuses_to_relink_a_user_to_a_different_id(self):
        original = uuid.uuid4()
        user = _user('priya', 'priya@jivo.in', auth_id=original)

        report = self.run_command('link_jivo_users', self.mapping(self.row(user, uuid.uuid4())))

        user.refresh_from_db()
        self.assertEqual(user.auth_id, original)
        self.assertIn(f'already linked to {original}', report)

    def test_running_again_is_harmless(self):
        user = _user('priya', 'priya@jivo.in')
        auth_id = uuid.uuid4()
        path = self.mapping(self.row(user, auth_id))

        self.run_command('link_jivo_users', path)
        report = self.run_command('link_jivo_users', path)

        user.refresh_from_db()
        self.assertEqual(user.auth_id, auth_id)
        self.assertIn('0 linked, 1 already linked', report)

    def test_skipped_rows_are_reported_not_applied(self):
        user = _user('priya', 'priya@jivo.in')
        row = {**self.row(user, uuid.uuid4(), status='skipped'), 'auth_id': None,
               'notes': ['email shared with another row']}

        report = self.run_command('link_jivo_users', self.mapping(row))

        user.refresh_from_db()
        self.assertIsNone(user.auth_id)
        self.assertIn('skipped at import', report)

    def test_dry_run_saves_nothing(self):
        user = _user('priya', 'priya@jivo.in')

        report = self.run_command(
            'link_jivo_users', self.mapping(self.row(user, uuid.uuid4())), '--dry-run')

        user.refresh_from_db()
        self.assertIsNone(user.auth_id)
        self.assertIn('Dry run, nothing saved: 1 linked', report)

    def test_refuses_the_output_of_an_import_dry_run(self):
        user = _user('priya', 'priya@jivo.in')

        with self.assertRaisesMessage(CommandError, 'dry run'):
            self.run_command(
                'link_jivo_users', self.mapping(self.row(user, uuid.uuid4()), dry_run=True))

        user.refresh_from_db()
        self.assertIsNone(user.auth_id)


class SyncJivoUsersTests(_TempDirTestCase):

    def sync(self, jivo_users, *args):
        with mock.patch(
            'users.management.commands.sync_jivo_users.AuthClient'
        ) as client:
            client.return_value.get_users.return_value = jivo_users
            return self.run_command('sync_jivo_users', *args)

    def test_full_name_joins_first_and_last(self):
        self.assertEqual(full_name({'first_name': 'Priya', 'last_name': 'Sharma'}), 'Priya Sharma')
        self.assertEqual(full_name({'first_name': ' Priya ', 'last_name': ''}), 'Priya')
        self.assertEqual(full_name({'first_name': '', 'last_name': None}), '')

    def test_refreshes_name_and_email_of_linked_users(self):
        auth_id = uuid.uuid4()
        user = _user('priya', 'Priya@jivo.in', name='Priya', auth_id=auth_id)

        self.sync([{'id': str(auth_id), 'email': 'priya@jivo.in',
                    'first_name': 'Priya', 'last_name': 'Sharma', 'is_active': True}])

        user.refresh_from_db()
        self.assertEqual(user.name, 'Priya Sharma')
        self.assertEqual(user.email, 'priya@jivo.in')
        self.assertEqual(user.username, 'priya')

    def test_a_nameless_jivo_account_keeps_the_local_name(self):
        auth_id = uuid.uuid4()
        user = _user('priya', 'priya@jivo.in', name='Priya Sharma', auth_id=auth_id)

        self.sync([{'id': str(auth_id), 'email': 'priya@jivo.in',
                    'first_name': '', 'last_name': '', 'is_active': True}])

        user.refresh_from_db()
        self.assertEqual(user.name, 'Priya Sharma')

    def test_never_touches_app_fields(self):
        auth_id = uuid.uuid4()
        user = _user('boss', 'boss@jivo.in', auth_id=auth_id, is_staff=True,
                     extra_pages=['Reports'])

        self.sync([{'id': str(auth_id), 'email': 'boss@jivo.in',
                    'first_name': 'Boss', 'last_name': '', 'is_active': False}])

        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertTrue(user.is_staff)
        self.assertEqual(user.extra_pages, ['Reports'])

    def test_create_missing_links_the_unlinked_row_with_that_email(self):
        user = _user('priya', 'priya@jivo.in')
        auth_id = uuid.uuid4()

        self.sync([{'id': str(auth_id), 'email': 'priya@jivo.in',
                    'first_name': 'Priya', 'last_name': '', 'is_active': True}],
                  '--create-missing')

        user.refresh_from_db()
        self.assertEqual(user.auth_id, auth_id)
        self.assertEqual(User.objects.count(), 1)

    def test_dry_run_saves_nothing(self):
        auth_id = uuid.uuid4()
        user = _user('priya', 'priya@jivo.in', name='Priya', auth_id=auth_id)

        self.sync([{'id': str(auth_id), 'email': 'priya@jivo.in',
                    'first_name': 'Priya', 'last_name': 'Sharma', 'is_active': True}],
                  '--dry-run')

        user.refresh_from_db()
        self.assertEqual(user.name, 'Priya')
