"""Make every local OMS password unusable — phase 6 of the Jivo Auth integration.

STAGED, NOT ACTIVE. This file lives in docs/jivo-auth-cleanup/ so that no
`manage.py migrate` applies it by accident: .117 is shared with an OMS that
still signs people in with these passwords. To apply it, follow
docs/jivo-auth-cleanup/README.md, which copies it to
users/migrations/ (renumbered after the latest migration then) as part of
the cleanup release.

Since the switch, Jivo Auth checks every password and OMS checks none:
`AUTHENTICATION_BACKENDS` lists only `JivoAuthBackend`. The old PBKDF2 hashes
are dead weight that a later misconfiguration (ModelBackend put back) would
bring back to life, so their values go. The column stays: Django's sessions
and admin need it.

Irreversible: the old hashes survive only in the backup taken first.

- One random unusable value per row (`make_password(None)`), not one shared
  `update()`, so no two users share a password value or a session hash.
- Rows that are already unusable (`!…`) are left alone.
- Admin sessions of the changed users end, because Django derives each
  session's check value from this column; those staff sign in again with Jivo.
"""
from django.contrib.auth.hashers import make_password
from django.db import migrations

USERNAME_FIELD = "username"   # historical models don't carry USERNAME_FIELD
BREAK_GLASS_USERNAMES = []    # decision D7: none (docs/jivo-auth-integration.md)


def disable_local_passwords(apps, schema_editor):
    User = apps.get_model("users", "User")
    users = (
        User.objects
        .exclude(**{f"{USERNAME_FIELD}__in": BREAK_GLASS_USERNAMES})
        .exclude(password__startswith="!")
    )
    batch = []
    for user in users.only("pk").iterator():
        user.password = make_password(None)   # a different random unusable value per row
        batch.append(user)
        if len(batch) == 1000:
            User.objects.bulk_update(batch, ["password"])
            batch = []
    User.objects.bulk_update(batch, ["password"])


class Migration(migrations.Migration):

    # Set to the latest users migration when this is copied into
    # users/migrations/ for the cleanup release.
    dependencies = [("users", "0038_user_auth_id")]

    operations = [migrations.RunPython(disable_local_passwords, migrations.RunPython.noop)]
