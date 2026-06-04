from django.db import migrations, models
import django.db.models.deletion


DEFAULT_RATE_CONDITIONS = ["BASIC_GT_MARKET"]


def create_default_flow_config(apps, schema_editor):
    OrderFlowConfig = apps.get_model("orders", "OrderFlowConfig")
    OrderFlowConfig.objects.get_or_create(
        id=1,
        defaults={
            "rate_approval_enabled": True,
            "billing_enabled": True,
            "auditor_enabled": True,
            "rate_conditions": DEFAULT_RATE_CONDITIONS,
        },
    )


def remove_default_flow_config(apps, schema_editor):
    OrderFlowConfig = apps.get_model("orders", "OrderFlowConfig")
    OrderFlowConfig.objects.filter(id=1).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0032_alter_categories_id_alter_dispatchlocation_id_and_more"),
        ("users", "0007_schemeproduct_partyproductassignment_scheme"),
    ]

    operations = [
        migrations.CreateModel(
            name="OrderFlowConfig",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("rate_approval_enabled", models.BooleanField(default=True)),
                ("billing_enabled", models.BooleanField(default=True)),
                ("auditor_enabled", models.BooleanField(default=True)),
                ("rate_conditions", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="updated_order_flow_configs",
                        to="users.user",
                    ),
                ),
            ],
            options={
                "verbose_name": "Order Flow Config",
                "verbose_name_plural": "Order Flow Config",
                "db_table": "order_flow_config",
            },
        ),
        migrations.RunPython(create_default_flow_config, remove_default_flow_config),
    ]
