"""Put the per-company conditions back to an EXACT match. Reverses 0008's rewrite.

0008 rewrote `company = 'OIL'` into a membership test, on the reasoning that
`company` had become a set. Running it proved the reasoning wrong:

    request for OIL,BEVERAGES
      -> matches the OIL workflow   (contains OIL)
      -> matches the BEVERAGES one  (contains BEVERAGES)
      -> AmbiguousWorkflowSelection

which makes every multi-company request unsubmittable. The engine is right to
refuse — WHICH chain approves a two-company request is a business decision, and
it will not invent one — so the fix is in the configuration, not the engine.

An exact match is the honest meaning of a per-company workflow: "this workflow
approves requests that are ONLY for OIL". With it:

    OIL alone           -> the OIL workflow, exactly as before
    OIL,BEVERAGES       -> WorkflowNotConfigured, with a message saying so

The second is a clear prompt to configure a workflow for multi-company
requests (an `ALL`-scoped one, or a condition that names the combination),
rather than a silent mis-route to whichever chain happened to sort first.

NOTE FOR A FRESH DATABASE: 0008 will rewrite these conditions and this
migration will put them back. That is deliberate — an applied migration is not
edited — and the end state is the same either way.
"""
from django.db import migrations

_MEMBERSHIP = "',' || company || ',' LIKE '%%,%s,%%'"
_EXACT = "company = '%s'"


def _to_exact(apps, schema_editor):
    _rewrite(apps, _MEMBERSHIP, _EXACT)


def _to_membership(apps, schema_editor):
    _rewrite(apps, _EXACT, _MEMBERSHIP)


def _rewrite(apps, old_template, new_template):
    from core.companies import COMPANY_CODES

    WorkflowQuery = apps.get_model('workflow', 'WorkflowQuery')
    for query in WorkflowQuery.objects.filter(workflow__module__code='BKDT'):
        text = query.query_text
        for code in COMPANY_CODES:
            text = text.replace(old_template % code, new_template % code)
        if text != query.query_text:
            query.query_text = text
            # `validated_at` is left alone: only the predicate changed, and
            # every check the validator makes reads the same before and after.
            query.save(update_fields=['query_text'])


class Migration(migrations.Migration):

    dependencies = [
        ('backdate', '0008_one_request_many_companies'),
    ]

    operations = [
        migrations.RunPython(_to_exact, _to_membership),
    ]
