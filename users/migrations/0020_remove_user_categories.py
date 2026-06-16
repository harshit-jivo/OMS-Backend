from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0019_user_categories"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="user",
            name="categories",
        ),
    ]
