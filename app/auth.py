import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime

from fastapi import Header, HTTPException, status

from app.config import settings


# Dependency guarding private routes — expects `Authorization: Bearer <INGEST_BEARER_TOKEN>`.
def require_bearer(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {settings.ingest_bearer_token}"
    # Compare bytes: compare_digest raises TypeError on a non-ASCII header, which would surface as a
    # 500 instead of a clean 401. Encoding keeps the comparison constant-time and total.
    if not hmac.compare_digest(authorization.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


# --- Admin session tokens ----------------------------------------------------
# The operator exchanges ADMIN_PASSWORD for a short-lived, HMAC-signed token (POST /admin/login),
# kept separate from the ingest bearer so a leak of one never grants the other. Stateless: the
# signing key is derived from the password, so no session store and a password change invalidates
# every outstanding token.


def _admin_signing_key() -> bytes | None:
    # None when no password is configured → every admin path fails closed.
    password = settings.admin_password
    if not password:
        return None
    return hashlib.sha256(b"admin-session|" + password.encode("utf-8")).digest()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str, key: bytes) -> str:
    return _b64url_encode(hmac.new(key, payload.encode("ascii"), hashlib.sha256).digest())


def issue_admin_token() -> tuple[str, datetime]:
    """Mint a signed `<payload>.<signature>` token; returns it with its expiry. Caller must have
    already verified the password. Raises if no admin password is configured."""
    key = _admin_signing_key()
    if key is None:
        raise RuntimeError("ADMIN_PASSWORD is not configured")
    expires_epoch = int(time.time()) + settings.admin_session_ttl_seconds
    payload = _b64url_encode(json.dumps({"exp": expires_epoch}).encode("utf-8"))
    return f"{payload}.{_sign(payload, key)}", datetime.fromtimestamp(expires_epoch, tz=UTC)


def verify_admin_token(token: str) -> bool:
    key = _admin_signing_key()
    if key is None:
        return False  # fail closed: no password configured → no valid sessions
    try:
        payload, signature = token.split(".", 1)
    except (ValueError, AttributeError):
        return False
    # Constant-time signature check before trusting any payload bytes.
    if not hmac.compare_digest(signature, _sign(payload, key)):
        return False
    try:
        exp = json.loads(_b64url_decode(payload)).get("exp")
    except (ValueError, TypeError, AttributeError):
        return False
    return isinstance(exp, int) and time.time() < exp


# Dependency guarding admin routes — expects `Authorization: Bearer <admin session token>`.
def require_admin(authorization: str = Header(default="")) -> None:
    scheme, _, token = authorization.partition(" ")
    if scheme != "Bearer" or not token or not verify_admin_token(token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
