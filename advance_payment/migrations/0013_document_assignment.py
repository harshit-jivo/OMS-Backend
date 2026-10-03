# Bills and POs sent to an Advance Payment User, who raises a request from them.

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('advance_payment', '0012_payout_tds'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='DocumentAssignment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('company', models.CharField(choices=[('OIL', 'Oil'), ('BEVERAGES', 'Beverages'), ('MART', 'Mart')],
                                             max_length=20)),
                ('kind', models.CharField(choices=[('BILL', 'A/P invoice'), ('PO', 'Purchase order'),
                                                   ('OTHER', 'Other open document'), ('LEDGER', 'Ledger item')],
                                          max_length=10)),
                ('sap_doc_entry', models.IntegerField()),
                ('sap_doc_num', models.CharField(blank=True, default='', max_length=30)),
                ('card_code', models.CharField(max_length=50)),
                ('card_name', models.CharField(blank=True, default='', max_length=200)),
                ('vendor_ref', models.CharField(blank=True, default='', max_length=100)),
                ('doc_date', models.DateField(blank=True, null=True)),
                ('due_date', models.DateField(blank=True, null=True)),
                ('doc_total', models.DecimalField(decimal_places=2, default=0, max_digits=19)),
                ('open_amount', models.DecimalField(decimal_places=2, default=0, max_digits=19)),
                ('note', models.TextField(blank=True, default='')),
                ('status', models.CharField(choices=[('OPEN', 'Open'), ('RAISED', 'Request raised'),
                                                     ('DISMISSED', 'Dismissed'), ('WITHDRAWN', 'Withdrawn')],
                                            default='OPEN', max_length=10)),
                ('created_on', models.DateTimeField(default=django.utils.timezone.now)),
                ('updated_on', models.DateTimeField(auto_now=True)),
                ('assigned_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+',
                                                  to=settings.AUTH_USER_MODEL)),
                ('assigned_to', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                                  related_name='payment_assignments', to=settings.AUTH_USER_MODEL)),
                ('request', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                              related_name='assignments', to='advance_payment.advancerequest')),
            ],
            options={
                'db_table': 'advance_payment_document_assignment',
                'ordering': ['-created_on'],
                'indexes': [models.Index(fields=['assigned_to', 'status'], name='ap_assignment_inbox_idx')],
                'constraints': [models.UniqueConstraint(condition=models.Q(('status', 'OPEN')),
                                                        fields=('company', 'kind', 'sap_doc_entry', 'assigned_to'),
                                                        name='ap_assignment_open_once')],
            },
        ),
    ]
