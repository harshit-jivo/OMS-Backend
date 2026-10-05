"""
Refresh the local copy of Jivo-owned fields for users with access to this
application, and optionally create local rows for those who haven't signed
in yet (so their app roles can be set up beforehand).

    python manage.py sync_jivo_users --dry-run
    python manage.py sync_jivo_users [--create-missing]

Needs JIVO_AUTH["API_KEY"]. Local is_active, roles and every app field are
never touched: losing access or deactivation in Jivo Auth already blocks
sign-in, and is only reported here.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from jivo_auth.client import AuthClient
from jivo_auth.exceptions import AuthServiceError
from jivo_auth.users import get_local_user

from users.jivo import full_name


# Adapt to the user model: Jivo field -> local model field, or None.
# OMS keeps one `name` instead of first/last names: "name" is Jivo's first
# and last name joined (users.jivo.full_name), so a Jivo last name isn't lost.
FIELDS = {
    "email": "email",
    "name": "name",
    "employee_code": None,
}

ID_FIELD = "auth_id"


class Command(BaseCommand):
    help = "Refresh local user records from Jivo Auth."

    def add_arguments(self, parser):
        parser.add_argument(
            "--create-missing",
            action="store_true",
            help="Create local rows for Jivo users with access who have none yet.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Report what would change, change nothing.")

    def handle(self, *args, create_missing, dry_run, **options):
        try:
            jivo_users = AuthClient().get_users()
        except AuthServiceError as exc:
            raise CommandError(f"Jivo Auth: {exc}") from exc

        User = get_user_model()
        local = {str(getattr(user, ID_FIELD)): user for user in User.objects.filter(**{f"{ID_FIELD}__isnull": False})}
        counts = {"updated": 0, "added locally": 0, "unchanged": 0, "not local yet": 0}

        with transaction.atomic():
            for jivo in jivo_users:
                # None, not "", for a nameless Jivo account: FIELDS skips None,
                # so it never blanks the local name.
                jivo = {**jivo, "name": full_name(jivo) or None}
                user = local.pop(jivo["id"], None)

                if user is None:
                    if not create_missing:
                        counts["not local yet"] += 1
                        continue

                    # Creates the row, or links an unlinked one by email when
                    # LOCAL_USER_LINK_BY_EMAIL is on.
                    user = get_local_user({"sub": jivo["id"], "email": jivo["email"]})
                    counts["added locally"] += 1
                    self.stderr.write(f"  added locally: {jivo['email']} (pk={user.pk})")

                changed = [
                    field
                    for key, field in FIELDS.items()
                    if field and jivo.get(key) is not None and getattr(user, field) != jivo[key]
                ]

                for key, field in FIELDS.items():
                    if field in changed:
                        setattr(user, field, jivo[key])

                if changed:
                    user.save(update_fields=changed)
                    counts["updated"] += 1
                else:
                    counts["unchanged"] += 1

                if not jivo.get("is_active", True):
                    self.stderr.write(f"  deactivated in Jivo Auth: {jivo['email']} (pk={user.pk})")

            if dry_run:
                transaction.set_rollback(True)

        for user in local.values():
            self.stderr.write(f"  no longer has access in Jivo Auth: pk={user.pk} {getattr(user, FIELDS['email'])}")

        prefix = "Dry run, nothing saved: " if dry_run else ""
        self.stderr.write(
            prefix
            + ", ".join(f"{count} {kind}" for kind, count in counts.items())
            + f", {len(local)} linked locally without access."
        )
