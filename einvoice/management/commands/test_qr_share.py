"""
Connectivity test for the IRN-QR shared folder (EINV_QR_SAVE_DIR).

Writes a small probe PNG to the share using the configured SMB credentials, then
reads it back and deletes it — so you can validate the path + credentials WITHOUT
generating a real IRN. Run it on the backend host (.75), where the app runs:

    python manage.py test_qr_share
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from einvoice import qr as qrgen


class Command(BaseCommand):
    help = "Write + read-back + delete a probe PNG in every company's QR folder to test access."

    def handle(self, *args, **opts):
        # One folder per company (OIL / BEVERAGE / MART), plus the legacy single
        # folder when no per-company mapping is configured.
        targets = dict(getattr(settings, "EINV_QR_SAVE_DIRS", None) or {})
        if not targets:
            legacy = getattr(settings, "EINV_QR_SAVE_DIR", "")
            if not legacy:
                self.stderr.write("No QR folders configured — nothing to test.")
                return
            targets = {"(default)": legacy}

        failures = 0
        for company_db, directory in targets.items():
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== {company_db} ==="))
            if not self._probe(directory):
                failures += 1

        self.stdout.write("")
        if failures:
            self.stderr.write(self.style.ERROR(f"{failures} of {len(targets)} folder(s) FAILED."))
        else:
            self.stdout.write(self.style.SUCCESS(f"All {len(targets)} QR folder(s) reachable and writable."))

    def _probe(self, directory):
        user = getattr(settings, "EINV_QR_SMB_USERNAME", "") or ""
        name = "__qr_share_probe__.png"
        png = qrgen.make_qr_png("QR-SHARE-CONNECTIVITY-TEST")

        self.stdout.write(f"Target : {directory}\\{name}")
        self.stdout.write(f"Auth   : {'SMB as ' + user if user else 'process account (no explicit creds)'}")

        try:
            if user:
                import ntpath
                import smbclient
                server = directory.strip("\\/").replace("/", "\\").split("\\")[0]
                smbclient.register_session(server, username=user,
                                           password=getattr(settings, "EINV_QR_SMB_PASSWORD", "") or "")
                path = ntpath.join(directory, name)
                with smbclient.open_file(path, mode="wb") as fh:
                    fh.write(png)
                size = len(smbclient.open_file(path, mode="rb").read())
                smbclient.remove(path)
            else:
                import os
                os.makedirs(directory, exist_ok=True)
                path = os.path.join(directory, name)
                with open(path, "wb") as fh:
                    fh.write(png)
                with open(path, "rb") as fh:
                    size = len(fh.read())
                os.remove(path)
        except Exception as exc:  # noqa: BLE001
            self.stderr.write(self.style.ERROR(f"FAILED: {type(exc).__name__}: {exc}"))
            self.stderr.write("Check: share path, username/password, and that the account has WRITE on the folder.")
            return False

        self.stdout.write(self.style.SUCCESS(
            f"OK — wrote, read back ({size} bytes), and deleted the probe. The share is reachable and writable."))
        return True
