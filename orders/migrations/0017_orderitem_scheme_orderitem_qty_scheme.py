from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0016_alter_dispatchlocation_options_alter_order_status'),
        ('users', '0008_remove_schemeproduct_product_schemeproduct_item_code'),
    ]

    operations = [
        migrations.AddField(
            model_name='orderitem',
            name='scheme',
            field=models.ForeignKey(
                blank=True,
                db_column='scheme_id',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='order_items',
                to='users.schemeproduct',
            ),
        ),
        migrations.AddField(
            model_name='orderitem',
            name='qty_scheme',
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                default=0,
                max_digits=10,
                null=True,
            ),
        ),
    ]
