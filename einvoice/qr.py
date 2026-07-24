"""
Render the NIC-signed QR string (IrnRecord.signed_qr_code, a JWS) into a QR
IMAGE for printing on the invoice.

NIC returns the *content* of the QR (the signed JWS) — we only turn it into a
scannable image. The QR must encode the SignedQRCode value verbatim so the NIC
QR-verifier app can validate the signature offline.

`qrcode` is imported lazily so the app still loads if the lib isn't installed;
the QR endpoints then fail with a clear message instead of breaking startup.
"""
from __future__ import annotations

import base64
import io


class QrUnavailable(RuntimeError):
    """Raised when the `qrcode` library isn't installed."""


def _qrcode():
    try:
        import qrcode  # noqa: WPS433 (lazy import by design)
        return qrcode
    except ImportError as exc:  # pragma: no cover
        raise QrUnavailable(
            "The 'qrcode' package is not installed. Run: pip install qrcode (Pillow is already a dependency)."
        ) from exc


def make_qr_png(data: str, *, box_size: int = 8, border: int = 2) -> bytes:
    """Return PNG bytes of a QR code encoding `data` (the SignedQRCode string)."""
    qrcode = _qrcode()
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_qr_data_uri(data: str, **kwargs) -> str:
    """Return a `data:image/png;base64,...` URI — handy to embed directly in HTML/JSON."""
    return "data:image/png;base64," + base64.b64encode(make_qr_png(data, **kwargs)).decode("ascii")
