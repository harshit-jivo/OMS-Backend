from django.apps import AppConfig


class WorkflowConfig(AppConfig):
    # BigAutoField to match the newest apps in the project (HAIS/payments);
    # workflow_action grows one row per approval action, so leave 64-bit room.
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'workflow'
    verbose_name = 'Workflow Engine'
