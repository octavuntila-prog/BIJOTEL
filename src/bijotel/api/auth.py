"""Bearer-token auth middleware for the BIJOTEL API.

Auth is OPT-IN: if neither the ``api_key`` constructor arg nor the
``BIJOTEL_API_KEY`` environment variable is set, the middleware is a
no-op pass-through (suitable for local development and tests). When a
key IS configured, every request must carry ``Authorization: Bearer
<key>``, with the following exceptions kept public so monitoring + docs
work for ops teams without a key:

* ``/health``  — liveness probes (Kubernetes, load balancers)
* ``/version`` — package version (forensic build trace)
* ``/docs``, ``/redoc`` — Swagger / ReDoc UIs
* ``/openapi.json`` — spec, needed by ``/docs`` itself

Key comparison uses :func:`hmac.compare_digest` to avoid timing-based
key recovery, even though the realistic threat model is "configured
secret, leaked via logs" rather than "remote attacker can measure ns".

Provenance: BIJOTEL-original, v1.1.0.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Routes that are ALWAYS public, no auth required.
# Kept as a frozenset for O(1) lookup on the hot path.
PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/version",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/docs/oauth2-redirect",
    }
)


def _is_public(path: str) -> bool:
    """True if ``path`` is in PUBLIC_PATHS or under /docs static assets."""
    if path in PUBLIC_PATHS:
        return True
    # Swagger UI loads a few static assets under /docs/* — keep them open
    # so the documentation page renders for unauthenticated visitors.
    return path.startswith("/docs/")


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Require ``Authorization: Bearer <api_key>`` when a key is configured.

    Args:
        app: ASGI application (passed by Starlette's middleware machinery).
        api_key: The expected key. If ``None``, falls back to
            ``$BIJOTEL_API_KEY``. If still ``None``, the middleware is a
            no-op (no auth enforced).
    """

    def __init__(
        self,
        app,
        api_key: str | None = None,
    ) -> None:
        super().__init__(app)
        resolved = api_key if api_key is not None else os.environ.get("BIJOTEL_API_KEY")
        # Treat the empty string the same as None — "set but blank" is almost
        # always a misconfiguration, not an intentional "block everything".
        self._api_key: str | None = resolved or None

    @property
    def required(self) -> bool:
        return self._api_key is not None

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Pass-through when no key is configured (dev mode)
        if not self.required:
            return await call_next(request)

        # Public paths are exempt
        if _is_public(request.url.path):
            return await call_next(request)

        # Extract Bearer token
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "Missing or malformed Authorization header. "
                    "Send 'Authorization: Bearer <key>'.",
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = auth_header[len("Bearer ") :].strip()

        # Constant-time comparison
        expected = self._api_key or ""
        if not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid API key."},
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await call_next(request)


__all__ = ["APIKeyMiddleware", "PUBLIC_PATHS"]
