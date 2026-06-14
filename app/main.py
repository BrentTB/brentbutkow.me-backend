from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.config import settings
from app.modules.contact.router import router as contact_router
from app.modules.recalls.router import router as recalls_router
from app.rate_limit import limiter

API_DESCRIPTION = """
**Recall Radar** — a live US food-recall API.

Ingests [openFDA](https://open.fda.gov/apis/food/enforcement/) food-enforcement reports, classifies
each by likely cause, and serves them to the [brentbutkow.me](https://brentbutkow.me) dashboard.

- Public reads are rate-limited to **60 requests/min per IP**.
- `POST /recalls/ingest` is **bearer-protected** (used by the daily ingest job).
"""

OPENAPI_TAGS = [
    {
        "name": "recalls",
        "description": "Food-recall data: list, aggregate stats, and the ingest trigger.",
    },
    {"name": "contact", "description": "Visitor contact messages."},
    {"name": "system", "description": "Operational endpoints (liveness)."},
]


def create_app() -> FastAPI:
    app = FastAPI(
        title="brentbutkow.me backend",
        summary="Live US food-recall API (Recall Radar).",
        description=API_DESCRIPTION,
        version="0.1.0",
        openapi_tags=OPENAPI_TAGS,
        contact={"name": "Brent Butkow", "url": "https://brentbutkow.me"},
        license_info={"name": "MIT"},
    )
    app.state.limiter = limiter
    # slowapi types its handler for RateLimitExceeded; Starlette's signature wants Exception.
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins,
        allow_origin_regex=settings.allowed_origin_regex,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["system"], summary="Liveness check")
    @limiter.exempt  # liveness probes must not be throttled by the global per-IP limit
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(recalls_router, prefix="/recalls", tags=["recalls"])
    app.include_router(contact_router, prefix="/contact", tags=["contact"])
    return app


app = create_app()
