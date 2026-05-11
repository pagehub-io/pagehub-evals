"""X-Twin-* override middleware (development-only).

Per the global CLAUDE.md `Twin override pattern`: each `*_BASE_URL` env
var has a header counterpart `X-Twin-{Title-Case}-Base-Url`. In
development the middleware extracts these into a request-scoped
contextvar that outbound HTTP helpers consult before falling back to
the env-var default. In any other environment the headers are
silently stripped before any handler sees them — staging is publicly
reachable too, so leaving the gate open would let any caller redirect
the service's outbound traffic (SSRF, exfil).
"""

from __future__ import annotations

import contextvars
from collections.abc import Mapping

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from api.config import get_settings

_twin_overrides_var: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "pagehub_evals_twin_overrides", default=None
)


def get_twin_overrides() -> Mapping[str, str]:
    return _twin_overrides_var.get() or {}


def resolve_base_url(env_var_name: str, default: str) -> str:
    """Outbound HTTP helpers should call this instead of reading env vars directly."""
    overrides = _twin_overrides_var.get() or {}
    return overrides.get(env_var_name, default)


def _header_to_env_name(header: str) -> str | None:
    """X-Twin-Tasks-Base-Url -> TASKS_BASE_URL. Returns None if not an X-Twin header."""
    if not header.lower().startswith("x-twin-"):
        return None
    rest = header[len("X-Twin-"):]
    return rest.upper().replace("-", "_")


class TwinMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        if settings.env != "development":
            # Strip silently. Don't log values — they may contain secrets.
            scrubbed = [(k, v) for k, v in request.scope.get("headers", []) if not k.lower().startswith(b"x-twin-")]
            request.scope["headers"] = scrubbed
            return await call_next(request)

        overrides: dict[str, str] = {}
        for raw_name, raw_value in request.headers.items():
            env_name = _header_to_env_name(raw_name)
            if env_name is None:
                continue
            overrides[env_name] = raw_value

        token = _twin_overrides_var.set(overrides)
        try:
            response: Response = await call_next(request)
        finally:
            _twin_overrides_var.reset(token)
        return response
