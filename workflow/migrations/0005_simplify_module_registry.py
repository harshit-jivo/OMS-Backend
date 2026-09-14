"""Reduce `workflow_modules` to identity: id, created_at, updated_at, code, name.

Drops `business_table`, `business_key_column`, `flow_table`, `flow_model` and
`is_active`. Module ROWS are preserved — only columns go — so every existing
registration, and every workflow hanging off it, survives untouched.

The one behaviour that would otherwise change silently is the key column.
`WorkflowQuery.effective_key_column` used to fall back to the module's
`business_key_column`; it now falls back to `'id'`. A module registered with
anything else (JSAP has `DocEntry`-style keys) would have its queries quietly
start binding the wrong column. `_carry_key_column_to_queries` copies the
module value down onto each query that was relying on the fallback, BEFORE the
column is dropped, so every existing query keeps the exact key it had.
"""
from django.db import migrations, models


def _carry_key_column_to_queries(apps, schema_editor):
    """Make each query's dependence on its module's key column explicit.

    Only touches queries that left `key_column` blank AND whose module used
    something other than `id`; a query that already names its column is
    already explicit, and `id` is the new default so writing it would be noise.
    """
    WorkflowQuery = apps.get_model('workflow', 'WorkflowQuery')
    updated = 0
    for query in WorkflowQuery.objects.select_related('workflow__module').filter(
            key_column=''):
        module_key = (query.workflow.module.business_key_column or '').strip()
        if module_key and module_key != 'id':
            query.key_column = module_key
            query.save(update_fields=['key_column'])
            updated += 1
    if updated:
        print(f'  carried module key column onto {updated} quer'
              f'{"y" if updated == 1 else "ies"}')


def _noop_reverse(apps, schema_editor):
    """Nothing to undo.

    Reversing re-adds the columns empty; the key columns written above stay on
    the queries, where they are still correct and still explicit.
    """


class Migration(migrations.Migration):

    dependencies = [
        ('workflow', '0004_config_is_active'),
    ]

    operations = [
        # Data first — it reads business_key_column, which the next operation
        # removes.
        migrations.RunPython(_carry_key_column_to_queries, _noop_reverse),
        migrations.RemoveField(
            model_name='workflowmodule',
            name='business_key_column',
        ),
        migrations.RemoveField(
            model_name='workflowmodule',
            name='business_table',
        ),
        migrations.RemoveField(
            model_name='workflowmodule',
            name='flow_model',
        ),
        migrations.RemoveField(
            model_name='workflowmodule',
            name='flow_table',
        ),
        migrations.RemoveField(
            model_name='workflowmodule',
            name='is_active',
        ),
        migrations.AlterField(
            model_name='workflowquery',
            name='key_column',
            field=models.CharField(blank=True, default='', help_text='Column this query exposes for document identity. Leave blank to use "id". Bound as a parameter, never interpolated.', max_length=60),
        ),
    ]
