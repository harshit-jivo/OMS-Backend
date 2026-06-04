from django.db import migrations, models


def create_billing_flow_config(apps, schema_editor):
    OrderFlowConfig = apps.get_model("orders", "OrderFlowConfig")
    OrderFlowConfig.objects.filter(id=1).update(flow_type="ASM")
    OrderFlowConfig.objects.get_or_create(
        flow_type="BILLING",
        defaults={
            "rate_approval_enabled": False,
            "billing_enabled": False,
            "auditor_enabled": True,
            "rate_conditions": [],
        },
    )


def remove_billing_flow_config(apps, schema_editor):
    OrderFlowConfig = apps.get_model("orders", "OrderFlowConfig")
    OrderFlowConfig.objects.filter(flow_type="BILLING").delete()


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("orders", "0033_orderflowconfig"),
    ]

    operations = [
        migrations.AddField(
            model_name="orderflowconfig",
            name="flow_type",
            field=models.CharField(default="ASM", max_length=20),
        ),
        migrations.RunPython(create_billing_flow_config, remove_billing_flow_config),
        migrations.AlterField(
            model_name="orderflowconfig",
            name="flow_type",
            field=models.CharField(default="ASM", max_length=20, unique=True),
        ),
    ]
