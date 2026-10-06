# Customer refunds (type CUSTOMER, Against Ledger / On Account) and the ledger
# items they are paid against. Ledger items are addressed the way SAP's
# PaymentInvoices addresses them: a DocEntry, or a journal TransId plus line,
# so a document is now unique per (request, kind, sap_doc_entry, sap_line).
# Existing rows get sap_line 0, which keeps them exactly as unique as before.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('advance_payment', '0010_budget_head_and_payment_purpose'),
    ]

    operations = [
        migrations.AlterField(
            model_name='advancerequest',
            name='request_type',
            field=models.CharField(choices=[('VENDOR', 'Vendor'), ('EMPLOYEE_ADVANCE', 'Employee'),
                                            ('EMPLOYEE_IMPREST', 'Employee Imprest'), ('CUSTOMER', 'Customer')],
                                   max_length=20),
        ),
        migrations.AlterField(
            model_name='advancerequest',
            name='payment_against',
            field=models.CharField(choices=[('ADVANCE', 'Advance'), ('AGAINST_BILL', 'Against Bill'),
                                            ('AGAINST_PO', 'Against PO'), ('ALL', 'All'),
                                            ('AGAINST_LEDGER', 'Against Ledger'), ('ON_ACCOUNT', 'On Account'),
                                            ('OTHER', 'Other')],
                                   max_length=20),
        ),
        migrations.AlterField(
            model_name='requestdocument',
            name='kind',
            field=models.CharField(choices=[('BILL', 'A/P invoice'), ('PO', 'Purchase order'),
                                            ('OTHER', 'Other open document'), ('LEDGER', 'Ledger item')],
                                   max_length=10),
        ),
        migrations.AddField(
            model_name='requestdocument',
            name='sap_line',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='requestdocument',
            name='sap_object',
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='requestdocument',
            name='direction',
            field=models.CharField(blank=True, choices=[('CREDIT', 'Credit'), ('DEBIT', 'Debit')], default='',
                                   max_length=6),
        ),
        migrations.RemoveConstraint(
            model_name='requestdocument',
            name='ap_request_document_once',
        ),
        migrations.AddConstraint(
            model_name='requestdocument',
            constraint=models.UniqueConstraint(fields=('request', 'kind', 'sap_doc_entry', 'sap_line'),
                                               name='ap_request_document_line_once'),
        ),
    ]
