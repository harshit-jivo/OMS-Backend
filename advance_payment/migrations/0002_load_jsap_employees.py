"""Load the employee master from JSAP's export, if it is on this machine.

The export is `advance_payment/data/jsap_employees.psv` ('|'-separated, first
line the header). It holds personal data, so it is NOT in git (see
.gitignore): copy it to that path by hand before migrating.

- File present: its rows are loaded.
- File missing: nothing is loaded and the migration still succeeds, so a
  server without the file can migrate. Load it later with
  `manage.py load_jsap_employees <file>`.

The file's columns: EmployeeId, EmployeeCode, EmployeeName, Email, Phone,
Designation, RoleTypeId, Gender, IsActive. "NULL" or an empty cell is no
value. Codes are stored upper-case; `created_on` is the day it is loaded.

Reversing it deletes exactly the rows in the file (by JSAP id), nothing else.
"""
from django.db import migrations

from advance_payment.services.employee_import import DEFAULT_FILE, read_rows


def load(apps, schema_editor):
    if not DEFAULT_FILE.exists():
        print(f'\n  advance_payment.0002: {DEFAULT_FILE} not found - no employees loaded. '
              'Load them later with: manage.py load_jsap_employees <file>')
        return
    Employee = apps.get_model('advance_payment', 'Employee')
    Employee.objects.bulk_create(
        [Employee(**fields) for fields in read_rows(DEFAULT_FILE)], batch_size=200)


def unload(apps, schema_editor):
    if not DEFAULT_FILE.exists():
        return
    Employee = apps.get_model('advance_payment', 'Employee')
    ids = [fields['employee_id'] for fields in read_rows(DEFAULT_FILE)]
    Employee.objects.filter(employee_id__in=ids).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('advance_payment', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(load, unload),
    ]
