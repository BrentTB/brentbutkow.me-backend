"""Response shapes every router documents the same way, so the OpenAPI schema stays consistent.

The per-route limit itself lives on the route's ``@limiter.limit`` decorator and in its
description — this block only says the response exists, so one router's copy cannot claim a number
another route does not use.
"""

from typing import Any

RATE_LIMITED: dict[int | str, dict[str, Any]] = {
    429: {"description": "Rate limit exceeded — see this endpoint's own limit."}
}
