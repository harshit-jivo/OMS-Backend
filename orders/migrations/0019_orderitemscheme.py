from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0010_remove_schemeproduct_state_schemeproduct_state_code_and_more'),
        ('orders', '0018_orderitem_is_scheme_visible'),
    ]

    operations = [
        migrations.CreateModel(
            name='OrderItemScheme',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('qty_scheme', models.DecimalField(blank=True, decimal_places=2, default=0, max_digits=10, null=True)),
                ('order_item', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='schemes', to='orders.orderitem')),
                ('scheme', models.ForeignKey(blank=True, db_column='scheme_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='order_item_schemes', to='users.schemeproduct')),
            ],
            options={
                'db_table': 'order_item_schemes',
            },
        ),
    ]
