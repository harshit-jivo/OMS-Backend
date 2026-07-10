from django.db import migrations, models


def copy_primary_category_to_categories(apps, schema_editor):
    User = apps.get_model("users", "User")

    for user in User.objects.exclude(category_id__isnull=True):
        user.categories.add(user.category_id)


def copy_first_category_to_primary(apps, schema_editor):
    User = apps.get_model("users", "User")

    for user in User.objects.filter(category_id__isnull=True):
        category = user.categories.first()
        if category:
            user.category_id = category.id
            user.save(update_fields=["category"])


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0024_alter_user_extra_pages"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="categories",
            field=models.ManyToManyField(blank=True, related_name="m2m_users", to="orders.categories"),
        ),
        migrations.RunPython(copy_primary_category_to_categories, copy_first_category_to_primary),
    ]
