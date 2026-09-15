"""One company per request again, and the SAP object NAME alongside its number.

COMPANY GOES BACK TO A SINGLE VALUE
-----------------------------------
A request covering several companies was one row that fanned out to one
`OPEN_BKDT` call per company. It is one company per request again: each grant
is approved on its own and written to its own SAP schema, so a refusal or a SAP
error in one cannot half-grant another.

The ACTION is untouched — `A`, `U` or both stay on one request, because
`OPEN_BKDT` has no action parameter and splitting the pair would write SAP rows
identical in every column SAP reads.

THIS MIGRATION REFUSES TO RUN while any multi-company row exists. There is no
honest automatic answer: keeping the first company silently drops rights
somebody asked for, and splitting the row into several invents requests that
nobody raised and that no approver has seen. Those rows have to be decided by a
person — delete them and raise them again, one per company.

`document_type_name` records the SAP object NAME as it read when the request
was raised. "13" means nothing to the person approving, and resolving the label
live would put a HANA lookup behind every list render. It is a LABEL only:
`document_type` is still what SAP is told, so a renamed object in SAP cannot
change what was granted — only how an old request reads.
"""
from django.db import migrations, models

from core.companies import COMPANY_CHOICES, COMPANY_CODES


def _refuse_multi_company(apps, schema_editor):
    BackDate = apps.get_model('backdate', 'BackDate')
    blocking = list(
        BackDate.objects.filter(company__contains=',')
        .values_list('pk', 'company'))
    if blocking:
        listed = ', '.join(f'#{pk} ({company})' for pk, company in blocking)
        raise RuntimeError(
            f'{len(blocking)} BackDate request(s) name more than one company: '
            f'{listed}. A request covers ONE company now, and neither keeping '
            f'the first nor splitting the row can be done without deciding '
            f'what was actually meant — delete these and raise them again, '
            f'one per company, then re-run this migration.')


class Migration(migrations.Migration):

    dependencies = [
        ('backdate', '0009_conditions_match_one_company'),
    ]

    operations = [
        migrations.RunPython(_refuse_multi_company, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name='backdate', name='backdate_company_valid'),
        migrations.AlterField(
            model_name='backdate',
            name='company',
            field=models.CharField(
                choices=COMPANY_CHOICES, db_index=True, max_length=20,
                help_text=('The company whose SAP database the rights are '
                           'granted in.')),
        ),
        migrations.AddConstraint(
            model_name='backdate',
            constraint=models.CheckConstraint(
                condition=models.Q(('company__in', list(COMPANY_CODES))),
                name='backdate_company_valid'),
        ),
        migrations.AddField(
            model_name='backdate',
            name='document_type_name',
            field=models.CharField(
                blank=True, default='', max_length=120,
                help_text=('SAP object name at the time of the request '
                           '(MOBJ.ObjName).')),
        ),
    ]
