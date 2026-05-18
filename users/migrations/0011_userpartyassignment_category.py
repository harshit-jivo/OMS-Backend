from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0010_remove_schemeproduct_state_schemeproduct_state_code_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='userpartyassignment',
            name='category',
            field=models.CharField(
                blank=True,
                choices=[('OIL', 'Oil'), ('BEVERAGES', 'Beverages'), ('MART', 'Mart')],
                db_index=True,
                max_length=20,
                null=True,
            ),
        ),
        migrations.AlterUniqueTogether(
            name='userpartyassignment',
            unique_together={('user', 'card_code', 'category')},
        ),
    ]
