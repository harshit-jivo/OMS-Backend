"""Generate a VAPID key pair for browser Web Push.

Usage::

    python manage.py generate_vapid_keys

Prints a fresh public/private VAPID key pair in ``.env`` format together with
copy-paste instructions. It NEVER writes any file (does not touch ``.env``), so
it is safe to run as many times as you like — each run just prints new keys.

The keys use the exact same format the running project already expects:
  * private key -- raw base64url-encoded P-256 scalar (loaded via
    ``py_vapid.Vapid.from_raw`` in ``orders/webpush.py``);
  * public key  -- base64url-encoded uncompressed EC point (the "application
    server key" the browser passes to ``pushManager.subscribe``).
"""

import base64

from cryptography.hazmat.primitives import serialization
from django.core.management.base import BaseCommand
from py_vapid import Vapid


def _b64url(raw: bytes) -> str:
    """base64url-encode without padding (Web Push / VAPID convention)."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class Command(BaseCommand):
    help = "Generate a VAPID public/private key pair and print it in .env format."

    def handle(self, *args, **options):
        # Reuse the project's VAPID library (py_vapid) to create the key pair.
        vapid = Vapid()
        vapid.generate_keys()

        private_value = vapid.private_key.private_numbers().private_value
        private_key = _b64url(private_value.to_bytes(32, "big"))
        public_key = _b64url(
            vapid.public_key.public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
        )

        out = self.stdout
        out.write("")
        out.write(self.style.SUCCESS("Generated a new VAPID key pair.\n"))

        out.write("Copy the following lines into your backend .env file:\n")
        out.write("")
        out.write(f"VAPID_PUBLIC_KEY={public_key}")
        out.write("")
        out.write(f"VAPID_PRIVATE_KEY={private_key}")
        out.write("")
        out.write("VAPID_ADMIN_EMAIL=mailto:admin@example.com")
        out.write("")

        out.write(self.style.WARNING("Instructions:"))
        out.write(
            "  1. Paste the three lines above into "
            "'OMS Backend/.env' (create it if missing)."
        )
        out.write(
            "  2. VAPID_PRIVATE_KEY is SECRET - never commit it or expose it to "
            "the browser."
        )
        out.write(
            "  3. VAPID_PUBLIC_KEY is safe to expose; the web client fetches it "
            "at subscribe time."
        )
        out.write(
            "  4. Set VAPID_ADMIN_EMAIL to a real contact address "
            "(the 'mailto:' prefix is optional)."
        )
        out.write("  5. Restart Django so the new keys are loaded.")
        out.write(
            "  6. NOTE: rotating these keys invalidates ALL existing browser "
            "subscriptions - users must re-subscribe (re-grant on next visit)."
        )
        out.write("")
