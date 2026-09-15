"""Re-point configured BKDT condition queries at the renamed table.

`workflow_queries.query_text` is CONFIGURATION, not code — administrators write
it through the Workflows page — and every BKDT condition names this module's
request table. Renaming `backdate_request` to `backdate` therefore broke them:
selection failed with "a configured workflow condition could not be evaluated",
which meant no new BackDate request could be submitted at all.

Code references are found by grep; this one is a row in a table, so it has to
be migrated like data. Only BKDT queries are touched, and only the exact
`backdate.backdate_request` relation — a query belonging to another module that
happens to mention the word is left alone.

`validated_at` is deliberately LEFT ALONE. Selection refuses to run a query
whose `validated_at` is NULL, so clearing it here would swap one broken state
for another. The rewrite is a relation RENAME and nothing else: every check the
validator makes — SELECT/WITH only, single statement, no DML keyword, no
forbidden schema, the key column present — reads the same before and after, so
the existing stamp is still true of the new text.
"""
from django.db import migrations

OLD = 'backdate.backdate_request'
NEW = 'backdate.backdate'


def _forward(apps, schema_editor):
    _rewrite(apps, OLD, NEW)


def _backward(apps, schema_editor):
    _rewrite(apps, NEW, OLD)


def _rewrite(apps, old, new):
    WorkflowQuery = apps.get_model('workflow', 'WorkflowQuery')
    for query in WorkflowQuery.objects.filter(
            workflow__module__code='BKDT', query_text__contains=old):
        query.query_text = query.query_text.replace(old, new)
        query.save(update_fields=['query_text'])


class Migration(migrations.Migration):

    dependencies = [
        ('backdate', '0006_align_field_metadata'),
        ('workflow', '0007_drop_runtime_and_testflow'),
    ]

    operations = [
        migrations.RunPython(_forward, _backward),
    ]
