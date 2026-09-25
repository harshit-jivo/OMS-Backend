"""Load (or refresh) the employee master from a JSAP employee export.

    python manage.py load_jsap_employees C:\\path\\to\\jsap_employees.psv
    python manage.py load_jsap_employees C:\\path\\to\\jsap_employees.psv --dry-run

Safe to run again. Rows are matched on JSAP's EmployeeId:
- new ids are added;
- existing ones get the file's values;
- an employee deleted in OMS stays deleted, and keeps its created date.

The file's format is in `advance_payment/services/employee_import.py`. It holds
personal data: keep it out of git and delete it from the server once loaded.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from advance_payment.models import Employee
from advance_payment.services.employee_import import ImportFileError, read_rows

#: Never overwritten on a refresh: OMS decides these once the row exists.
KEEP_ON_UPDATE = ('is_deleted', 'created_on')


class Command(BaseCommand):
    help = 'Load or refresh the employee master from a JSAP employee export (.psv).'

    def add_arguments(self, parser):
        parser.add_argument('file', help='Path to the JSAP employee export (.psv).')
        parser.add_argument('--dry-run', action='store_true',
                            help='Read and count, change nothing.')

    def handle(self, *args, **options):
        try:
            rows = read_rows(options['file'])
        except FileNotFoundError as exc:
            raise CommandError(f'File not found: {options["file"]}') from exc
        except ImportFileError as exc:
            raise CommandError(str(exc)) from exc

        existing = {e.employee_id: e for e in Employee.objects.filter(
            employee_id__in=[r['employee_id'] for r in rows])}
        new = [r for r in rows if r['employee_id'] not in existing]
        changed = [r for r in rows if r['employee_id'] in existing]

        self.stdout.write(f'{len(rows)} rows read: {len(new)} new, {len(changed)} already loaded.')
        if options['dry_run']:
            self.stdout.write('Dry run: nothing changed.')
            return

        with transaction.atomic():
            Employee.objects.bulk_create([Employee(**r) for r in new], batch_size=200)
            for fields in changed:
                employee = existing[fields['employee_id']]
                for name, value in fields.items():
                    if name not in KEEP_ON_UPDATE:
                        setattr(employee, name, value)
                employee.save()
        self.stdout.write(self.style.SUCCESS(
            f'Done: {len(new)} added, {len(changed)} updated.'))
