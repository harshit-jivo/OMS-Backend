"""Drop `stored_name` and `original_name`; `stored_path` becomes the identity.

`stored_name` was exactly the basename of `stored_path` — verified across every
row before this ran, 17 of 17 matching — so it stored nothing the path did not
already carry. It survives as a derived property on the model, because the
storage layer still needs a filename for the download's MIME type.

`original_name` was the upload's own filename. In practice a camera supplies a
UUID: 14 of the 15 rows on record were named like
'e2b8c640-039c-4ed3-8c38-42794dc661ba.jpeg'. It labelled nothing a person could
use, and the UI now names attachments by type ("Cheque image", "Deposit slip"),
so nothing reads it.

ORDER MATTERS HERE, and it is not the order Django generated.

The autodetector put the two drops first and the UNIQUE constraint last. On a
database with duplicate paths that fails at the last step — after the columns
are already gone — leaving no way to tell the duplicates apart. Adding the
constraint FIRST means such a database is refused with both columns intact.

The check in front of it is the same idea made explicit: a clear error naming
the offending paths beats a raw IntegrityError from the constraint.
"""
from django.db import migrations, models


def guard_unique_paths(apps, schema_editor):
    """Refuse to proceed if two attachments claim the same path."""
    Attachment = apps.get_model('attachments', 'Attachment')
    seen, clashing = set(), set()
    for path in (Attachment.objects
                 .values_list('stored_path', flat=True).iterator()):
        key = (path or '').strip()
        if key in seen:
            clashing.add(key)
        seen.add(key)
    if clashing:
        raise RuntimeError(
            'Cannot make stored_path unique: these paths are used by more '
            'than one attachment — %s. Resolve them before migrating; '
            'dropping stored_name first would remove the only other way to '
            'tell the rows apart.' % sorted(clashing))


class Migration(migrations.Migration):

    dependencies = [
        ('attachments', '0003_backfill_stored_path'),
    ]

    operations = [
        migrations.RunPython(guard_unique_paths, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='attachment',
            name='stored_path',
            field=models.CharField(max_length=500, unique=True),
        ),
        migrations.RemoveField(
            model_name='attachment',
            name='stored_name',
        ),
        migrations.RemoveField(
            model_name='attachment',
            name='original_name',
        ),
    ]
