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
    help = "Write + read-back + delete a probe PNG in EINV_QR_SAVE_DIR to test access."

    def handle(self, *args, **opts):
        directory = getattr(settings, "EINV_QR_SAVE_DIR", "")
        if not directory:
            self.stderr.write("EINV_QR_SAVE_DIR is empty — nothing to test.")
            return

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
            return

        self.stdout.write(self.style.SUCCESS(
            f"OK — wrote, read back ({size} bytes), and deleted the probe. The share is reachable and writable."))
