"""Remove HAIS (Hardware Asset Identification Software) from the database.

Kept separate from 0028 on purpose: dropping a schema is the only destructive
step in this revert, and splitting it lets you apply the scheme-v2/combo revert
without it, or hold it back for a release of its own.

`DROP SCHEMA ... CASCADE` cannot be undone by a migration -- the reverse is a
no-op that leaves the schema absent. It is safe here because every HAIS table
was empty when this was written (all six of tbl_Asset, tbl_AssetType,
tbl_AssetLog, tbl_Departments, tbl_StorageType and tbl_AssetStorageType held
0 rows), so nothing is lost. VERIFY THAT IS STILL TRUE BEFORE APPLYING:

    SELECT 'tbl_Asset', COUNT(*) FROM hais."tbl_Asset"
    UNION ALL SELECT 'tbl_AssetLog', COUNT(*) FROM hais."tbl_AssetLog";

The `hais` rows in django_migrations are left alone: the app is gone, so Django
ignores them, and deleting them would only matter if HAIS were ever restored --
in which case they are the record of what was already applied.
"""

from django.db import migrations


def remove_hais_role(apps, schema_editor):
    """Drop the 'hais' role added by 0026_hais_role."""
    UserRole = apps.get_model("users", "UserRole")
    UserRole.objects.filter(name="hais").delete()


def restore_hais_role(apps, schema_editor):
    UserRole = apps.get_model("users", "UserRole")
    UserRole.objects.update_or_create(
        name="hais",
        defaults={"display_name": "HAIS", "is_active": True},
    )


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0028_revert_combo"),
    ]

    operations = [
        migrations.RunPython(remove_hais_role, restore_hais_role),
        migrations.RunSQL(
            sql="DROP SCHEMA IF EXISTS hais CASCADE;",
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
