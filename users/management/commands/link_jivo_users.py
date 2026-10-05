"""
Store Jivo user IDs from the output of Jivo Auth's `manage.py import_users`.

    python manage.py link_jivo_users /secure/tmp/mapping.json --dry-run
    python manage.py link_jivo_users /secure/tmp/mapping.json

Sets auth_id on the local user whose primary key is each row's source_id,
and nothing else. Rows it can't apply safely are reported, not changed.
Running it again is harmless.
"""

import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


ID_FIELD = "auth_id"


class Command(BaseCommand):
    help = "Set auth_id from Jivo Auth's import_users mapping."

    def add_arguments(self, parser):
        parser.add_argument("path", help="The JSON import_users printed.")
        parser.add_argument("--dry-run", action="store_true", help="Report what would change, change nothing.")

    def handle(self, *args, path, dry_run, **options):
        try:
            with open(path, encoding="utf-8") as handle:
                mapping = json.load(handle)
            rows = mapping["users"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CommandError(f"Can't read {path}: {exc}") from exc

        if mapping.get("dry_run"):
            raise CommandError("This is the output of an import dry run: its IDs don't exist in Jivo Auth.")

        User = get_user_model()
        email_field = User.get_email_field_name()
        counts = {"linked": 0, "already linked": 0, "skipped at import": 0, "conflict": 0}

        def report(kind, row, reason):
            counts[kind] += 1
            self.stderr.write(f"  {kind}: source_id={row.get('source_id')} {row.get('email')}: {reason}")

        with transaction.atomic():
            for row in rows:
                auth_id = row.get("auth_id")

                if row.get("status") not in ("created", "linked") or not auth_id:
                    report("skipped at import", row, "; ".join(row.get("notes") or []))
                    continue

                user = User.objects.select_for_update().filter(pk=row["source_id"]).first()

                if user is None:
                    report("conflict", row, "no local user with this primary key")
                    continue

                current = getattr(user, ID_FIELD)

                if current is not None:
                    if str(current) == auth_id:
                        counts["already linked"] += 1
                    else:
                        report("conflict", row, f"already linked to {current}")
                    continue

                local_email = (getattr(user, email_field) or "").strip().lower()

                if local_email != (row.get("email") or "").lower():
                    report("conflict", row, f"local email is now {local_email!r}; export and import again")
                    continue

                other = User.objects.filter(**{ID_FIELD: auth_id}).exclude(pk=user.pk).first()

                if other is not None:
                    report("conflict", row, f"auth_id already used by local user pk={other.pk}")
                    continue

                User.objects.filter(pk=user.pk).update(**{ID_FIELD: auth_id})
                counts["linked"] += 1

            if dry_run:
                transaction.set_rollback(True)

        prefix = "Dry run, nothing saved: " if dry_run else ""
        self.stderr.write(prefix + ", ".join(f"{count} {kind}" for kind, count in counts.items()) + ".")

        if counts["conflict"]:
            self.stderr.write("Resolve the conflicts above with the application's owner, then run again.")
