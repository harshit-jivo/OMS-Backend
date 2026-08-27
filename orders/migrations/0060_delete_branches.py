"""Remove the duplicate `orders.Branches` model. STATE ONLY — no SQL.

`branches` is a real table with 22 rows, owned and written by the SAP sync
through `sap_sync.Branch`. `orders.Branches` was a SECOND model pointed at the
same table, and wrong about every column of it:

    column      live table            sap_sync.Branch   orders.Branches
    bpl_id      integer               IntegerField      CharField(50)
    bpl_name    varchar(200)          max_length=200    max_length=100
    category    varchar(20)           max_length=20     max_length=50
    is_active   boolean not null      declared          absent
    created_at  timestamptz not null  declared          absent
    updated_at  timestamptz not null  declared          absent

Both models were `managed = False`, so Django has never issued CREATE, ALTER or
DROP for this table from either of them, and `DeleteModel` on an unmanaged
model is a no-op at the database level: it removes the model from Django's
migration STATE and emits nothing. `python manage.py sqlmigrate orders 0060`
prints no statements — that is the check that this is safe, and it is worth
re-running rather than trusting this comment.

The table, its 22 rows and every column are untouched.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0059_merge_20260826_branches"),
    ]

    operations = [
        migrations.DeleteModel(name="Branches"),
    ]
