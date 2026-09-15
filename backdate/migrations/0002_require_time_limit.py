"""`time_limit` becomes required.

SAP's posting validator (`SBO_SP_TRANSACTIONNOTIFICATION`) ends every one of
its 14 BKDT lookups with `AND CURRENT_TIMESTAMP < r."timeLimit"`. A NULL makes
that comparison UNKNOWN, so the row never matches: the request is approved, the
SAP write "succeeds", and the user still cannot post. Refusing the NULL at the
column is the only place that guarantee holds for every writer.

WRITTEN BY HAND, deliberately. `makemigrations` prompts for a value to fill
existing NULLs with, and there is no honest answer: an invented expiry would
silently grant or revoke real posting rights. This migration therefore fails
loudly if any NULL exists, which is the correct outcome — those rows have to be
decided by a person, not by a default.
"""
from django.db import migrations, models


def _refuse_null_time_limits(apps, schema_editor):
    BackDateRequest = apps.get_model('backdate', 'BackDateRequest')
    count = BackDateRequest.objects.filter(time_limit__isnull=True).count()
    if count:
        raise RuntimeError(
            f'{count} BackDate request(s) have no time_limit. SAP ignores a '
            f'grant with a NULL expiry, so these rows cannot be made valid by '
            f'a default — set an expiry on each one, then re-run this '
            f'migration.')


class Migration(migrations.Migration):

    dependencies = [
        ('backdate', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(
            _refuse_null_time_limits, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='backdaterequest',
            name='time_limit',
            field=models.DateTimeField(
                help_text='When the granted rights lapse in SAP.'),
        ),
    ]
