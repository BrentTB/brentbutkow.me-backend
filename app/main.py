from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from app.config import settings
from app.modules.recalls.router import router as recalls_router


def client_ip(request: Request) -> str:
    # Behind a proxy the peer is the proxy, so without this every client shares one rate-limit
    # bucket. Read the real client from the proxy-controlled end of X-Forwarded-For — the entry
    # `trusted_proxy_hops` from the right, which the closest trusted proxy appends and a client
    # cannot forge past. With no trusted proxy configured, fall back to the direct peer.
    hops = settings.trusted_proxy_hops
    if hops > 0:
        forwarded = request.headers.get("x-forwarded-for", "")
        chain = [ip.strip() for ip in forwarded.split(",") if ip.strip()]
        if len(chain) >= hops:
            return chain[-hops]
    return get_remote_address(request)


limiter = Limiter(key_func=client_ip, default_limits=["60/minute"])


def create_app() -> FastAPI:
    app = FastAPI(title="brentbutkow.me backend")
    app.state.limiter = limiter
    # slowapi types its handler for RateLimitExceeded; Starlette's signature wants Exception.
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    @limiter.exempt  # liveness probes must not be throttled by the global per-IP limit
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(recalls_router, prefix="/recalls", tags=["recalls"])
    return app


app = create_app()
