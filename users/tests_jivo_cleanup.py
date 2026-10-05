"""Tests for the staged phase 6 cleanup migration.

`docs/jivo-auth-cleanup/0039_disable_local_passwords.py` is staged outside
`users/migrations/` until the cleanup release (see that folder's README), so
no `migrate` runs it early. These tests load that exact file and run its
function against the test database, so the file that will be applied is the
file that was tested.

Run with::

    python manage.py test users.tests_jivo_cleanup --settings=OMS.test_settings
"""
import importlib.util
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.test import TestCase

from users.models import User

STAGED = Path(settings.BASE_DIR) / 'docs' / 'jivo-auth-cleanup' / '0039_disable_local_passwords.py'


def _staged_migration():
    spec = importlib.util.spec_from_file_location('staged_disable_local_passwords', STAGED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DisableLocalPasswordsTests(TestCase):

    def setUp(self):
        self.module = _staged_migration()
        self.users = [
            User.objects.create_user(username=f'u{i}', password='CorrectHorse9!', name=f'u{i}')
            for i in range(3)
        ]

    def run_migration(self):
        self.module.disable_local_passwords(apps, None)

    def test_it_is_staged_not_active(self):
        """It must not sit in users/migrations/ until the cleanup release."""
        active = Path(settings.BASE_DIR) / 'users' / 'migrations'
        self.assertFalse(list(active.glob('*disable_local_passwords*')))

    def test_every_password_becomes_unusable(self):
        self.run_migration()

        for user in User.objects.all():
            self.assertFalse(user.has_usable_password(), user.username)
            self.assertFalse(user.check_password('CorrectHorse9!'), user.username)

    def test_each_row_gets_its_own_value(self):
        self.run_migration()

        values = list(User.objects.values_list('password', flat=True))
        self.assertEqual(len(values), len(set(values)))

    def test_rows_already_unusable_are_left_alone(self):
        already = User.objects.create(username='jivo-only', name='jivo-only',
                                      password=make_password(None))
        before = already.password

        self.run_migration()

        already.refresh_from_db()
        self.assertEqual(already.password, before)

    def test_break_glass_usernames_keep_their_password(self):
        self.module.BREAK_GLASS_USERNAMES = ['u0']

        self.run_migration()

        self.assertTrue(User.objects.get(username='u0').check_password('CorrectHorse9!'))
        self.assertFalse(User.objects.get(username='u1').has_usable_password())

    def test_nothing_but_the_password_changes(self):
        def everything_but_password():
            return list(User.objects.order_by('pk').values(
                *[f.attname for f in User._meta.concrete_fields if f.name != 'password']))

        before = everything_but_password()
        self.run_migration()
        self.assertEqual(everything_but_password(), before)
