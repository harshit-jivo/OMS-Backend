from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sap_sync', '0019_alter_partyaddress_unique_together'),
    ]

    operations = [
        # This DB user's search_path is `payments,public`, so an unqualified
        # CREATE TABLE lands in `payments` — where every other app table lives in
        # `public`. Force `public` for this migration's transaction so the table
        # is created alongside the rest (SET LOCAL is scoped to the migration's
        # atomic transaction, so it does not leak to later migrations/queries).
        migrations.RunSQL(
            sql="SET LOCAL search_path TO public;",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.CreateModel(
            name='SalesCancelledLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('order_id', models.CharField(blank=True, max_length=100, null=True)),
                ('sap_doc_entry', models.IntegerField(blank=True, null=True)),
                ('sap_doc_num', models.IntegerField(blank=True, null=True)),
                ('status', models.CharField(choices=[('STARTED', 'Started'), ('SUCCESS', 'Success'), ('FAILED', 'Failed')], default='STARTED', max_length=10)),
                ('cancellation_reason', models.TextField(blank=True, null=True)),
                ('request_data', models.JSONField(blank=True, null=True)),
                ('response_data', models.JSONField(blank=True, null=True)),
                ('error_message', models.TextField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={
                'db_table': 'sales_cancellation_logs',
                'ordering': ['-created_at'],
            },
        ),
    ]
