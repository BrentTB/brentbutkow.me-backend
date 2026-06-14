import hmac

from fastapi import Header, HTTPException, status

from app.config import settings


# Dependency guarding private routes — expects `Authorization: Bearer <INGEST_BEARER_TOKEN>`.
def require_bearer(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {settings.ingest_bearer_token}"
    # Compare bytes: compare_digest raises TypeError on a non-ASCII header, which would surface as a
    # 500 instead of a clean 401. Encoding keeps the comparison constant-time and total.
    if not hmac.compare_digest(authorization.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
