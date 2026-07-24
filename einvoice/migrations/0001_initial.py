from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="IrnRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("environment", models.CharField(choices=[("sandbox", "Sandbox"), ("production", "Production")], default="sandbox", max_length=10)),
                ("order_id", models.BigIntegerField(blank=True, db_index=True, null=True)),
                ("source", models.CharField(blank=True, max_length=50, null=True)),
                ("supplier_gstin", models.CharField(max_length=15)),
                ("doc_type", models.CharField(choices=[("INV", "Invoice"), ("CRN", "Credit Note"), ("DBN", "Debit Note")], max_length=3)),
                ("doc_no", models.CharField(db_index=True, max_length=16)),
                ("doc_date", models.DateField()),
                ("financial_year", models.CharField(max_length=7)),
                ("buyer_gstin", models.CharField(blank=True, max_length=15, null=True)),
                ("generation_status", models.CharField(choices=[("PENDING", "Pending"), ("GENERATED", "Generated"), ("FAILED", "Failed"), ("CANCELLED", "Cancelled")], db_index=True, default="PENDING", max_length=20)),
                ("irn", models.CharField(blank=True, max_length=64, null=True, unique=True)),
                ("ack_no", models.CharField(blank=True, max_length=20, null=True)),
                ("ack_date", models.DateTimeField(blank=True, null=True)),
                ("signed_invoice", models.TextField(blank=True, null=True)),
                ("signed_qr_code", models.TextField(blank=True, null=True)),
                ("irp_status", models.CharField(blank=True, max_length=3, null=True)),
                ("ewb_no", models.CharField(blank=True, max_length=15, null=True)),
                ("ewb_date", models.DateTimeField(blank=True, null=True)),
                ("cancelled_at", models.DateTimeField(blank=True, null=True)),
                ("cancel_reason_code", models.CharField(blank=True, max_length=1, null=True)),
                ("cancel_remarks", models.CharField(blank=True, max_length=100, null=True)),
                ("request_payload", models.JSONField(blank=True, null=True)),
                ("response_payload", models.JSONField(blank=True, null=True)),
                ("error_details", models.JSONField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "IRN Record",
                "db_table": "einvoice_irn",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="EwayBill",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("environment", models.CharField(choices=[("sandbox", "Sandbox"), ("production", "Production")], default="sandbox", max_length=10)),
                ("order_id", models.BigIntegerField(blank=True, db_index=True, null=True)),
                ("ewb_no", models.CharField(blank=True, max_length=15, null=True, unique=True)),
                ("ewb_date", models.DateTimeField(blank=True, null=True)),
                ("valid_till", models.DateTimeField(blank=True, null=True)),
                ("ewb_status", models.CharField(blank=True, max_length=3, null=True)),
                ("supplier_gstin", models.CharField(blank=True, max_length=15, null=True)),
                ("buyer_gstin", models.CharField(blank=True, max_length=15, null=True)),
                ("doc_type", models.CharField(blank=True, max_length=15, null=True)),
                ("doc_no", models.CharField(blank=True, max_length=16, null=True)),
                ("doc_date", models.DateField(blank=True, null=True)),
                ("trans_mode", models.CharField(blank=True, max_length=1, null=True)),
                ("trans_distance", models.IntegerField(blank=True, null=True)),
                ("transporter_id", models.CharField(blank=True, max_length=15, null=True)),
                ("transporter_name", models.CharField(blank=True, max_length=100, null=True)),
                ("trans_doc_no", models.CharField(blank=True, max_length=15, null=True)),
                ("trans_doc_date", models.DateField(blank=True, null=True)),
                ("vehicle_no", models.CharField(blank=True, max_length=15, null=True)),
                ("vehicle_type", models.CharField(blank=True, max_length=1, null=True)),
                ("part_b_updated", models.BooleanField(default=False)),
                ("generation_status", models.CharField(choices=[("PENDING", "Pending"), ("GENERATED", "Generated"), ("FAILED", "Failed"), ("CANCELLED", "Cancelled")], db_index=True, default="PENDING", max_length=20)),
                ("cancelled_at", models.DateTimeField(blank=True, null=True)),
                ("cancel_reason_code", models.CharField(blank=True, max_length=2, null=True)),
                ("cancel_remarks", models.CharField(blank=True, max_length=100, null=True)),
                ("request_payload", models.JSONField(blank=True, null=True)),
                ("response_payload", models.JSONField(blank=True, null=True)),
                ("error_details", models.JSONField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("irn_record", models.ForeignKey(blank=True, db_column="irn_record_id", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="eway_bills", to="einvoice.irnrecord")),
            ],
            options={
                "verbose_name": "e-Way Bill",
                "db_table": "einvoice_ewaybill",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="irnrecord",
            constraint=models.UniqueConstraint(
                fields=("environment", "supplier_gstin", "financial_year", "doc_type", "doc_no"),
                name="einvoice_irn_doc_uq",
            ),
        ),
    ]
