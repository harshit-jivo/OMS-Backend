"""Record the preview path for checks that already have one on disk.

`preview_image` arrived in 0006, so every check run before it is blank — and
the history page fell back to the uploaded file, which is a PDF for almost
every label. A PDF in an `<img>` is a broken image.

The previews themselves were never missing: `service.save_preview` has written
one on every check since the pipeline was rebuilt, at a path derived from the
upload's name. This recovers that path rather than re-rendering anything, so
it needs no poppler, no Gemini and no network — it is a filename lookup.

Rows whose preview file is genuinely absent are left blank on purpose. The
serializer then returns '' and the page says the artwork is unavailable, which
is true, instead of showing a broken image.
"""
import os

from django.db import migrations


def backfill(apps, schema_editor):
    from django.core.files.storage import default_storage

    LabelData = apps.get_model('legal', 'LabelData')

    updated = []
    for row in LabelData.objects.filter(preview_image='').exclude(label_file=''):
        stem = os.path.splitext(os.path.basename(row.label_file.name))[0]
        candidate = f'labels/previews/{stem}.png'
        try:
            if not default_storage.exists(candidate):
                continue
            row.preview_image = default_storage.url(candidate)
        except Exception:  # noqa: BLE001 — a storage backend may refuse either
            continue
        updated.append(row)

    if updated:
        LabelData.objects.bulk_update(updated, ['preview_image'], batch_size=200)


def unbackfill(apps, schema_editor):
    """Nothing to undo: 0006 removes the column itself on a full rollback, and
    clearing paths that point at real files would lose information for no
    reason."""


class Migration(migrations.Migration):

    dependencies = [
        ('legal', '0006_label_check_history'),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
