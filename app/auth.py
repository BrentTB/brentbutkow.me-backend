import hmac

from fastapi import Header, HTTPException, status

from app.config import settings


# Dependency guarding private routes — expects `Authorization: Bearer <INGEST_BEARER_TOKEN>`.
def require_bearer(authorization: str = Header(default="")) -> None:
    expected = f"Bearer {settings.ingest_bearer_token}"
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
