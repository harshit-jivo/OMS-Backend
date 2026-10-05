"""
Export this application's users for Jivo Auth's `manage.py import_users`.

    python manage.py export_jivo_users /secure/tmp/users.json
    python manage.py export_jivo_users /secure/tmp/users.json --include-inactive

Only users without an auth_id are exported, so running it again picks up
just the ones still to migrate. The file contains password hashes: it's
written with mode 600, never inside the project, and should be deleted
once the import is verified.
"""

import json
import os
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


# Adapt to the user model: model field for each exported value, or None.
# OMS keeps one `name` instead of first/last names, so the whole of it goes
# to Jivo Auth as first_name.
FIELDS = {
    "email": "email",
    "first_name": "name",
    "last_name": None,
    "employee_code": None,
}

ID_FIELD = "auth_id"


class Command(BaseCommand):
    help = "Write users without an auth_id as JSON for Jivo Auth's import_users."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Output file, outside the project directory.")
        parser.add_argument("--include-inactive", action="store_true", help="Also export inactive users.")

    def handle(self, *args, path, include_inactive, **options):
        output = Path(path).resolve()

        if output.is_relative_to(Path(settings.BASE_DIR).resolve()):
            raise CommandError("Write the export outside the project: it contains password hashes.")

        User = get_user_model()
        unlinked = User.objects.filter(**{f"{ID_FIELD}__isnull": True}).order_by("pk")
        field_names = {field.name for field in User._meta.get_fields()}
        users, inactive = unlinked, 0

        if not include_inactive and "is_active" in field_names:
            users = unlinked.filter(is_active=True)
            inactive = unlinked.filter(is_active=False).count()

        rows, without_email = [], []

        for user in users.iterator():
            email = (getattr(user, FIELDS["email"]) or "").strip()

            if not email:
                without_email.append(user)
                continue

            row = {
                "source_id": user.pk,
                "email": email,
                "password": user.password or None,
                "is_active": bool(getattr(user, "is_active", True)),
            }

            for key in ("first_name", "last_name", "employee_code"):
                if FIELDS[key]:
                    row[key] = getattr(user, FIELDS[key]) or ""

            rows.append(row)

        try:
            # O_EXCL: never overwrite; 0o600: owner-only, it holds password hashes.
            fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise CommandError(f"{output} exists; choose another path or delete it first.") from exc

        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(rows, handle, indent=2, default=str)

        by_email = {}

        for row in rows:
            by_email.setdefault(row["email"].lower(), []).append(row["source_id"])

        shared = {email: ids for email, ids in by_email.items() if len(ids) > 1}

        self.stderr.write(
            f"{unlinked.count()} users without {ID_FIELD}: {len(rows)} exported to {output}, "
            f"{len(without_email)} without an email, {inactive} inactive (not exported)."
        )

        for user in without_email:
            self.stderr.write(f"  no email: pk={user.pk} {user.get_username()}")

        for email, ids in shared.items():
            self.stderr.write(f"  same email, import_users will skip all of them: {email} pk={ids}")
