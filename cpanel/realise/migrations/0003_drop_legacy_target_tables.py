"""Drop C_Panel's two older target stores and their lookup tables.

TargetMaster (main group / state / person targets) and SegmentTarget (a flat
target per dimension), plus the MainGroupMaster / StateMaster lookups only
TargetMaster used. Targets live in TargetNode, written by the Targets page;
these were read only as fallbacks behind it and were empty in production
(MainGroupMaster held just the seven channel names it seeded itself). The
dashboard answers exactly as before — services.get_channel_target_map and
get_segment_target_map return what those empty fallbacks did.

TargetMaster points at the two lookups, so it goes first.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('cp_realise', '0002_c_panel_schema'),
    ]

    operations = [
        migrations.DeleteModel(name='TargetMaster'),
        migrations.DeleteModel(name='SegmentTarget'),
        migrations.DeleteModel(name='MainGroupMaster'),
        migrations.DeleteModel(name='StateMaster'),
    ]
