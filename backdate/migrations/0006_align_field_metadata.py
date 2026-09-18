"""Help text and a dropped FK help string, caught up with the models.

No column changes: `company` and `sap_username` gain their `help_text`, and
`flow.workflow` loses the help string that described the matched query it no
longer stores. Django emits `ALTER TABLE` for these because they are field
declarations, but the resulting SQL leaves the data untouched.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('backdate', '0005_drop_stale_indexes'),
        ('workflow', '0007_drop_runtime_and_testflow'),
    ]

    operations = [
        migrations.AlterField(
            model_name='backdate',
            name='company',
            field=models.CharField(choices=[('OIL', 'Oil'), ('BEVERAGES', 'Beverages'), ('MART', 'Mart')], db_index=True, help_text='The company whose SAP database the rights are granted in.', max_length=20),
        ),
        migrations.AlterField(
            model_name='backdate',
            name='sap_username',
            field=models.CharField(help_text='SAP user the rights are granted to (OUSR.USER_CODE).', max_length=20),
        ),
        migrations.AlterField(
            model_name='backdateflow',
            name='workflow',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+', to='workflow.workflow'),
        ),
    ]
