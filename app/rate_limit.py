from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import settings


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


# Counters live in-process (slowapi's default in-memory store): correct for the current
# single-instance deploy, but they reset on restart and aren't shared across workers — so before
# scaling to multiple instances, point this at a shared backend (storage_uri="redis://...").
limiter = Limiter(key_func=client_ip, default_limits=["60/minute"])
