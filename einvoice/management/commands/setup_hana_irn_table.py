"""
Create the EINVOICE_IRN mirror table in a HANA company schema.

Run once per schema you want to mirror into (defaults to HANA_COMPANY_DB):

    python manage.py setup_hana_irn_table --schema JIVO_OIL_HANADB
    python manage.py setup_hana_irn_table            # uses HANA_COMPANY_DB

Requires the HANA user (DATABASES['hana'].USER) to have CREATE TABLE rights in
that schema. Safe to re-run — it skips if the table already exists.
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from einvoice import hana_store


class Command(BaseCommand):
    help = "Create the EINVOICE_IRN mirror table (+ QR_PNG) in a HANA company schema."

    def add_arguments(self, parser):
        parser.add_argument("--schema", default=None, help="HANA schema (default: HANA_COMPANY_DB)")

    def handle(self, *args, **opts):
        from hana.services.connection import HANAConnection

        schema = opts["schema"] or settings.HANA_COMPANY_DB
        if not schema:
            self.stderr.write("No schema given and HANA_COMPANY_DB is empty.")
            return
        self.stdout.write(f"Ensuring {schema}.{hana_store.TABLE} …")
        with HANAConnection() as conn:
            if hana_store.table_exists(conn, schema):
                self.stdout.write(self.style.WARNING("  already exists — nothing to do."))
                return
            hana_store.create_table(conn, schema)
        self.stdout.write(self.style.SUCCESS(f"  created {schema}.{hana_store.TABLE} (with QR_PNG BLOB)."))
