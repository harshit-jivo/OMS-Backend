"""Drop this app's first-port tables (`control_panel_*`).

They were never used — the Control Panel's data is in the cp_realise /
cp_dashboard apps, in the `c_panel` schema — and are empty. Tables that point
at others go first (TargetMaster -> MainGroupMaster / StateMaster).
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('control_panel', '0001_initial'),
    ]

    operations = [
        migrations.DeleteModel(name='TargetMaster'),
        migrations.DeleteModel(name='SegmentTarget'),
        migrations.DeleteModel(name='TargetNode'),
        migrations.DeleteModel(name='MonthlyTarget'),
        migrations.DeleteModel(name='ExpenseBudget'),
        migrations.DeleteModel(name='MainGroupMaster'),
        migrations.DeleteModel(name='StateMaster'),
    ]
