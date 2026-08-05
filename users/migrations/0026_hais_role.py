from django.db import migrations


def create_hais_role(apps, schema_editor):
    """Add the HAIS role so users can be assigned to it (idempotent)."""
    UserRole = apps.get_model("users", "UserRole")
    UserRole.objects.get_or_create(
        name="hais",
        defaults={"display_name": "HAIS", "is_active": True},
    )


def remove_hais_role(apps, schema_editor):
    UserRole = apps.get_model("users", "UserRole")
    UserRole.objects.filter(name="hais").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0025_user_categories"),
    ]

    operations = [
        migrations.RunPython(create_hais_role, remove_hais_role),
    ]
