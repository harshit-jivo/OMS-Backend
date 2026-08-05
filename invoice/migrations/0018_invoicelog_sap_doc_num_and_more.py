from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('invoice', '0017_remove_creditlimitlogs_id_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='invoicelog',
            name='sap_doc_num',
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name='invoicelog',
            name='sap_doc_entry',
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
    ]
