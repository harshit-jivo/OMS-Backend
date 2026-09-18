"""One request may name several companies; the flow keeps the SAP payload.

`company` stops being one code and becomes the SET a request asked for —
`'OIL'`, `'OIL,BEVERAGES'` — which is the shape JSAP stored (`branch = '1,2'`).
Asking for the same rights in two companies is ONE business decision approved
ONCE, so it is one row with one flow. The fan-out moves to the SAP layer, where
`OPEN_BKDT` takes one branch per call.

Existing single-company rows are already valid under the new rule: `'OIL'` is a
one-element set. Nothing is rewritten.

`sap_payload` is added to the flow because one request can now produce several
SAP calls which can disagree — OIL accepted, BEVERAGES refused — and neither
what was sent nor what came back is reproducible from the request afterwards.

The configured condition queries are re-pointed in the same migration: they
tested `company = 'OIL'`, which stops matching the moment a request names two
companies. See `_repoint_conditions` for the exact rewrite.
"""
from django.conf import settings
from django.db import migrations, models


#: `company = 'OIL'` matched when a request named exactly one company. It stops
#: matching `'OIL,BEVERAGES'`, so every BKDT condition is rewritten to ask
#: whether the company is IN the set. Delimiting both sides is what keeps a
#: company from matching another whose name contains it.
_OLD = "company = '%s'"
_NEW = "',' || company || ',' LIKE '%%,%s,%%'"


def _repoint_conditions(apps, schema_editor):
    _rewrite(apps, forward=True)


def _restore_conditions(apps, schema_editor):
    _rewrite(apps, forward=False)


def _rewrite(apps, forward):
    from core.companies import COMPANY_CODES

    WorkflowQuery = apps.get_model('workflow', 'WorkflowQuery')
    for query in WorkflowQuery.objects.filter(workflow__module__code='BKDT'):
        text = query.query_text
        for code in COMPANY_CODES:
            old, new = _OLD % code, _NEW % code
            if not forward:
                old, new = new, old
            text = text.replace(old, new)
        if text != query.query_text:
            query.query_text = text
            # `validated_at` is left alone on purpose: selection refuses a
            # query whose stamp is NULL, and every check the validator makes —
            # SELECT only, single statement, no DML, no forbidden schema, the
            # key column present — reads the same before and after. Only the
            # predicate changed.
            query.save(update_fields=['query_text'])


class Migration(migrations.Migration):

    dependencies = [
        ('backdate', '0007_repoint_condition_queries'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='backdate',
            name='backdate_company_valid',
        ),
        migrations.AddField(
            model_name='backdateflow',
            name='sap_payload',
            field=models.JSONField(blank=True, default=None, null=True),
        ),
        migrations.AlterField(
            model_name='backdate',
            name='company',
            field=models.CharField(db_index=True, help_text='Companies the rights are granted in, comma-separated (e.g. "OIL,BEVERAGES").', max_length=64),
        ),
        migrations.AddConstraint(
            model_name='backdate',
            constraint=models.CheckConstraint(condition=models.Q(('company__regex', '^(OIL|BEVERAGES|MART)(,(OIL|BEVERAGES|MART))*$')), name='backdate_company_valid'),
        ),
        migrations.RunPython(_repoint_conditions, _restore_conditions),
    ]
