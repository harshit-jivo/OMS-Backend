# Generated manually – rename price fields on OrderItem

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0038_alter_categories_id_alter_dispatchlocation_id_and_more'),
    ]

    operations = [
        # Step 1: basic_price → price_list_basic  (must happen first to avoid collision)
        migrations.RenameField(
            model_name='orderitem',
            old_name='basic_price',
            new_name='price_list_basic',
        ),
        # Step 2: market_price → basic_price
        migrations.RenameField(
            model_name='orderitem',
            old_name='market_price',
            new_name='basic_price',
        ),
    ]
