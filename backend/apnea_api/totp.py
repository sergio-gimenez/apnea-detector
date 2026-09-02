"""RFC 6238 time-based one-time passwords, standard library only.

Deliberately tiny and self-contained so the second factor can be audited at a
glance. SHA-1 and a 30-second step with 6 digits are the defaults every
authenticator app assumes; changing them would break existing enrolments.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote, urlencode

DIGITS = 6
PERIOD = 30


def generate_secret(length: int = 20) -> str:
    """A fresh base32 secret (no padding), the form authenticator apps expect."""
    return base64.b32encode(secrets.token_bytes(length)).decode("ascii").rstrip("=")


def _hotp(secret: str, counter: int) -> str:
    padding = "=" * (-len(secret) % 8)
    key = base64.b32decode(secret.upper() + padding)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFF_FFFF
    return str(code % (10**DIGITS)).zfill(DIGITS)


def now_code(secret: str, at: float | None = None) -> str:
    """The code valid right now. Used by tests and never sent to a client."""
    return _hotp(secret, int((at if at is not None else time.time()) // PERIOD))


def match_step(secret: str, code: str, *, window: int = 1, at: float | None = None) -> int | None:
    """The time step ``code`` belongs to (current ± ``window``), or ``None``.

    The window absorbs clock skew between server and phone. Comparison is
    constant-time so a wrong guess leaks nothing through timing. Callers persist
    the returned step to refuse a code that has already been spent.
    """
    if not secret or not code or not code.isdigit() or len(code) != DIGITS:
        return None
    base = int((at if at is not None else time.time()) // PERIOD)
    for offset in range(-window, window + 1):
        if hmac.compare_digest(_hotp(secret, base + offset), code):
            return base + offset
    return None


def verify(secret: str, code: str, *, window: int = 1, at: float | None = None) -> bool:
    """True when ``code`` matches the current step or one within ``window`` of it."""
    return match_step(secret, code, window=window, at=at) is not None


def provisioning_uri(secret: str, account: str, issuer: str = "Nocturne") -> str:
    """``otpauth://`` URI for the enrolment QR code."""
    label = quote(f"{issuer}:{account}", safe=":")
    query = urlencode(
        {
            "secret": secret,
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": PERIOD,
        }
    )
    return f"otpauth://totp/{label}?{query}"
