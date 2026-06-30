import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.db import engine
from app.internal.router import router as internal_router
from app.modules.admin.router import router as admin_router
from app.modules.contact.router import router as contact_router
from app.modules.nullspace.router import router as nullspace_router
from app.modules.recalls.router import router as recalls_router
from app.rate_limit import limiter
from app.subscriptions.router import router as subscriptions_router

logger = logging.getLogger(__name__)

API_DESCRIPTION = """
**Recall Radar** — a live food-recall API covering the US, UK, and South Africa.

Ingests [openFDA](https://open.fda.gov/apis/food/enforcement/) and USDA FSIS (US), UK
[FSA](https://data.food.gov.uk/food-alerts/) food alerts, and South Africa NCC recall notices,
classifies each by likely cause, and serves them to the
[brentbutkow.me](https://brentbutkow.me) dashboard.

- Public reads are rate-limited to **60 requests/min per IP**.
- The `POST /recalls/ingest/*` endpoints are **bearer-protected** (used by the daily ingest job).
"""

OPENAPI_TAGS = [
    {
        "name": "recalls",
        "description": "Food-recall data: list, aggregate stats, and the ingest trigger.",
    },
    {"name": "contact", "description": "Visitor contact messages."},
    {"name": "nullspace", "description": "Null Space game leaderboard."},
    {
        "name": "subscriptions",
        "description": "Recall alert subscriptions: create, confirm, manage, unsubscribe.",
    },
    {"name": "internal", "description": "Internal job triggers (ingest-driven alert dispatch)."},
    {"name": "admin", "description": "Operator admin API (session-token protected)."},
    {"name": "system", "description": "Operational endpoints (liveness)."},
]


def create_app() -> FastAPI:
    app = FastAPI(
        title="brentbutkow.me backend",
        summary="Live US + UK + South Africa food-recall API (Recall Radar).",
        description=API_DESCRIPTION,
        version="0.1.0",
        openapi_tags=OPENAPI_TAGS,
        contact={"name": "Brent Butkow", "url": "https://brentbutkow.me"},
        license_info={"name": "MIT"},
    )
    app.state.limiter = limiter
    # slowapi types its handler for RateLimitExceeded; Starlette's signature wants Exception.
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    @app.exception_handler(OperationalError)
    def _postgres_unavailable(request: Request, exc: OperationalError) -> JSONResponse:
        # The engine connects lazily, so a down/unreachable Postgres surfaces here on the first
        # query of a request rather than at startup. Translate the noisy driver traceback into a
        # short 503 instead of leaking a 500 with the full SQLAlchemy/psycopg stack. The port/host
        # stays in the server log; the client gets a generic message — don't disclose topology.
        port = engine.url.port or 5432
        logger.warning("Postgres instance not reachable on port %s, ensure it is running", port)
        return JSONResponse(
            status_code=503,
            content={"detail": "Database temporarily unavailable, please retry shortly."},
        )

    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins,
        allow_origin_regex=settings.allowed_origin_regex,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.api_route("/health", methods=["GET", "HEAD"], tags=["system"], summary="Liveness check")
    @limiter.exempt  # liveness probes must not be throttled by the global per-IP limit
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(recalls_router, prefix="/recalls", tags=["recalls"])
    app.include_router(contact_router, prefix="/contact", tags=["contact"])
    app.include_router(nullspace_router, prefix="/nullspace", tags=["nullspace"])
    app.include_router(subscriptions_router, prefix="/subscriptions", tags=["subscriptions"])
    app.include_router(internal_router, prefix="/internal", tags=["internal"])
    app.include_router(admin_router, prefix="/admin", tags=["admin"])
    return app


app = create_app()
