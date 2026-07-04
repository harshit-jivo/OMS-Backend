"""
Cryptographic helpers for the NIC e-Invoice (IRN) API.

The API never accepts/returns plain JSON for the sensitive parts. The scheme is:

  Auth request:
    - generate a random 32-byte AppKey (AES-256 key)
    - put AppKey (base64) inside the credentials JSON
    - RSA-encrypt the whole JSON with the GST *public key*  (PKCS#1 v1.5)
    - base64 the result -> {"Data": "..."}

  Auth response:
    - response "Data" is the SEK (session key), AES-encrypted with *your AppKey*
    - AES-ECB-decrypt it with the AppKey to recover the SEK (still base64)

  Every subsequent call (e.g. generate IRN):
    - AES-ECB-encrypt the request payload with the SEK
    - AES-ECB-decrypt the response "Data" with the SEK

AES here is AES-256 in ECB mode with PKCS#7 padding (this is what NIC uses).
"""
from __future__ import annotations

import base64
import os

from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad


# ---- AppKey ----------------------------------------------------------------

def generate_app_key() -> bytes:
    """A fresh random 32-byte AES-256 key used for the auth handshake."""
    return os.urandom(32)


# ---- RSA (auth payload, encrypted with the GST public key) -----------------

def load_public_key(path: str) -> RSA.RsaKey:
    """
    Load the GST public key. Accepts a PEM public key, a PEM certificate,
    or a DER (.cer) certificate. RSA.import_key handles PEM/DER public keys
    and X.509 certs directly in recent pycryptodome.
    """
    with open(path, "rb") as fh:
        data = fh.read()
    return RSA.import_key(data)


def rsa_encrypt(plaintext: bytes, public_key: RSA.RsaKey) -> str:
    """RSA/ECB/PKCS1Padding encrypt -> base64 string."""
    cipher = PKCS1_v1_5.new(public_key)
    encrypted = cipher.encrypt(plaintext)
    return base64.b64encode(encrypted).decode("utf-8")


# ---- AES-256-ECB / PKCS7 (everything keyed by AppKey or SEK) ----------------

def aes_encrypt(plaintext: bytes, key: bytes) -> str:
    """AES-256-ECB encrypt with PKCS7 padding -> base64 string."""
    cipher = AES.new(key, AES.MODE_ECB)
    encrypted = cipher.encrypt(pad(plaintext, AES.block_size))
    return base64.b64encode(encrypted).decode("utf-8")


def aes_decrypt(b64_ciphertext: str, key: bytes) -> bytes:
    """base64 -> AES-256-ECB decrypt -> PKCS7-unpad -> plaintext bytes."""
    cipher = AES.new(key, AES.MODE_ECB)
    decrypted = cipher.decrypt(base64.b64decode(b64_ciphertext))
    return unpad(decrypted, AES.block_size)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("utf-8")


def unb64(text: str) -> bytes:
    return base64.b64decode(text)
