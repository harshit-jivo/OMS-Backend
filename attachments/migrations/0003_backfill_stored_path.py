"""Fill `stored_path` for rows written before the column existed.

Every legacy row was written by the same code path — `directory_for()` plus the
UUID name — so the current configuration IS where those files are, and joining
the two reconstructs the path exactly. That equivalence is only true right now:
the moment a share is repointed, the old rows become unreconstructable. Which is
the whole reason the column exists, and the reason this backfill runs
immediately after the column is added rather than later.

Not a guess dressed as data. If a directory is not configured, the row is left
blank rather than filled with a path built from an empty string — a blank falls
back to the old behaviour and still resolves; a wrong path would be worse than
none, because it would look authoritative.

NO FILE IS TOUCHED. This reads settings and writes one text column. Nothing is
moved, renamed, opened or deleted, and a wrong value here can only send a reader
back to the fallback it used before.

Reverse blanks the column, so re-running forwards recomputes cleanly instead of
skipping rows it has already filled.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    Attachment = apps.get_model('attachments', 'Attachment')
    # Imported here, not at module scope: a migration must not fail to load
    # because an app module moved after it was written.
    from attachments.storage import directory_for
    from django.core.exceptions import ValidationError
    import ntpath
    import os

    for attachment in Attachment.objects.filter(stored_path='').iterator():
        try:
            directory = directory_for(attachment.attachment_type)
        except ValidationError:
            # Share not configured — see the docstring. Leave it blank.
            continue
        join = ntpath.join if str(directory).startswith('\\\\') else os.path.join
        attachment.stored_path = join(directory, attachment.stored_name)
        attachment.save(update_fields=['stored_path'])


def backwards(apps, schema_editor):
    Attachment = apps.get_model('attachments', 'Attachment')
    Attachment.objects.update(stored_path='')


class Migration(migrations.Migration):

    dependencies = [
        ('attachments', '0002_attachment_stored_path'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
