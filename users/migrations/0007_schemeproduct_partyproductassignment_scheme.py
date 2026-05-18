import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0016_alter_dispatchlocation_options_alter_order_status'),
        ('users', '0006_alter_user_options_user_created_by_user_updated_by_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='SchemeProduct',
            fields=[
                ('scheme_id', models.AutoField(primary_key=True, serialize=False)),
                ('scheme_name', models.CharField(max_length=255)),
                ('is_active', models.BooleanField(db_column='isactive', default=True)),
                ('item_code', models.ForeignKey(db_column='product_id', on_delete=django.db.models.deletion.PROTECT, related_name='scheme_products', to='orders.productdetails')),
                ('state', models.ForeignKey(db_column='state_id', on_delete=django.db.models.deletion.PROTECT, related_name='scheme_products', to='users.state')),
            ],
            options={
                'db_table': 'scheme_product',
                'ordering': ['scheme_name'],
            },
        ),
        migrations.AddField(
            model_name='partyproductassignment',
            name='is_scheme',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='partyproductassignment',
            name='scheme',
            field=models.ForeignKey(blank=True, db_column='scheme_id', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='party_product_assignments', to='users.schemeproduct'),
        ),
    ]
