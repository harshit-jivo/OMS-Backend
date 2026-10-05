"""Move the Control Panel's tables into their own schema, `c_panel`.

They were created by 0001 wherever the connection's search_path put new tables
first (OMS's `payments` schema). Each is moved with ALTER TABLE ... SET SCHEMA —
rows, indexes, constraints and its id sequence go with it — and given C_Panel's
own table name (`dashboard_<model>`). The model state follows with
AlterModelTable; see cpanel/core/db.py.
"""
from django.db import migrations

from cpanel.core.db import c_panel_table


class Migration(migrations.Migration):

    dependencies = [
        ('cp_dashboard', '0001_initial'),
        ('cp_realise', '0002_c_panel_schema'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[migrations.AlterModelTable('expensebudget', c_panel_table('dashboard_expensebudget'))],
            database_operations=[migrations.RunSQL(
                'ALTER TABLE "cp_dashboard_expensebudget" SET SCHEMA c_panel; ALTER TABLE c_panel."cp_dashboard_expensebudget" RENAME TO "dashboard_expensebudget";',
                reverse_sql='ALTER TABLE c_panel."dashboard_expensebudget" RENAME TO "cp_dashboard_expensebudget"; ALTER TABLE c_panel."cp_dashboard_expensebudget" SET SCHEMA public;',
            )],
        ),
    ]
