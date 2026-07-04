"""
Offline tests for the crypto layer — no network, no real credentials.
Run with:  python manage.py test einvoice
"""
import json
import os
import tempfile

from django.test import SimpleTestCase

from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

from . import crypto


class CryptoRoundTripTests(SimpleTestCase):
    def test_aes_round_trip(self):
        key = crypto.generate_app_key()
        ct = crypto.aes_encrypt(b"hello world payload", key)
        self.assertEqual(crypto.aes_decrypt(ct, key), b"hello world payload")

    def test_sek_handshake_simulation(self):
        # Server AES-encrypts the SEK with our AppKey; we decrypt it back.
        app_key = crypto.generate_app_key()
        sek = os.urandom(32)
        enc_sek = crypto.aes_encrypt(crypto.b64(sek).encode(), app_key)
        recovered = json.loads('{"Sek":"%s"}' % crypto.aes_decrypt(enc_sek, app_key).decode())
        self.assertEqual(crypto.unb64(recovered["Sek"]), sek)

    def test_sek_recovered_once_standard_string_format(self):
        # Standard wire format: Data is base64 AES(authJSON, AppKey); the inner
        # "Sek" is plaintext base64 and must NOT be AES-decrypted again.
        from einvoice.client import EInvoiceClient
        app_key = crypto.generate_app_key()
        real_sek = os.urandom(32)
        auth_json = {
            "AuthToken": "TOK123",
            "Sek": crypto.b64(real_sek),
            "TokenExpiry": "2026-06-20 11:31:25",
        }
        data = crypto.aes_encrypt(json.dumps(auth_json).encode(), app_key)
        body = {"Status": "1", "Data": data}
        session = EInvoiceClient()._session_from_body(body, app_key)
        self.assertEqual(session.sek, real_sek)
        self.assertEqual(session.auth_token, "TOK123")

    def test_sek_recovered_once_object_format(self):
        # Portal/display format: Data is an object whose "Sek" field is itself
        # AES-encrypted with the AppKey.
        from einvoice.client import EInvoiceClient
        app_key = crypto.generate_app_key()
        real_sek = os.urandom(32)
        body = {
            "Status": "1",
            "Data": {
                "AuthToken": "TOK456",
                # Server encrypts the RAW session-key bytes (not its base64).
                "Sek": crypto.aes_encrypt(real_sek, app_key),
                "TokenExpiry": "2026-06-20 11:31:25",
            },
        }
        session = EInvoiceClient()._session_from_body(body, app_key)
        self.assertEqual(session.sek, real_sek)
        self.assertEqual(session.auth_token, "TOK456")

    def test_rsa_encrypt_with_public_key(self):
        priv = RSA.generate(2048)
        with tempfile.NamedTemporaryFile("wb", suffix=".pem", delete=False) as fh:
            fh.write(priv.publickey().export_key())
            path = fh.name
        try:
            pub = crypto.load_public_key(path)
            b64ct = crypto.rsa_encrypt(b"secret-credentials", pub)
            dec = PKCS1_v1_5.new(priv).decrypt(crypto.unb64(b64ct), None)
            self.assertEqual(dec, b"secret-credentials")
        finally:
            os.remove(path)
