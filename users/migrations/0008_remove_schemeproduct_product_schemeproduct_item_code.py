import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sap_sync', '0010_alter_product_table'),
        ('users', '0007_schemeproduct_partyproductassignment_scheme'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.RemoveField(
                    model_name='schemeproduct',
                    name='item_code',
                ),
                migrations.AddField(
                    model_name='schemeproduct',
                    name='item_code',
                    field=models.ForeignKey(
                        db_column='item_code',
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name='scheme_products',
                        to='sap_sync.product',
                    ),
                ),
            ],
            database_operations=[],
        ),
    ]
