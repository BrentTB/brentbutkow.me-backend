from __future__ import annotations

import base64
import hashlib
import secrets


def generate_confirmation_token() -> tuple[str, str]:
    """
    Returns (raw_token, sha256_hex_hash).
    The raw token is NEVER stored; only the hash is persisted.
    """
    raw = secrets.token_urlsafe(32)
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    return raw, hashed


def generate_management_token() -> str:
    """
    Returns a 43-char base64url token derived from 32 cryptographically random bytes.

    32 bytes → base64url encodes to ceil(32 / 3) * 4 = 44 chars, with one trailing '='.
    Stripping that one '=' yields exactly 43 chars.
    """
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
