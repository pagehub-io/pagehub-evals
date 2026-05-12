"""Body-size guard for ``POST /v1/fixtures/import``.

Starlette/FastAPI has **no default request-body limit** — the whole body is
buffered and JSON-decoded into ``FixtureBundle`` before the route handler
runs, so a route-level ``Content-Length`` check fires too late to bound the
decode (and a chunked / no-``Content-Length`` request bypasses it entirely).
This middleware reads the import-path request body off the wire itself,
chunk by chunk, and short-circuits with ``413`` the moment more than
``MAX_BUNDLE_BYTES`` have arrived — before any parsing. Bodies under the cap
are replayed downstream unchanged (a 1 MiB ceiling makes the double buffer a
non-issue).
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BUNDLE_BYTES = 1_048_576  # 1 MiB

_GUARDED_PATH = "/v1/fixtures/import"


class FixtureBodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != _GUARDED_PATH:
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        total = 0
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.disconnect":
                # Propagate the disconnect downstream as an empty body.
                more_body = False
                break
            body = message.get("body", b"")
            total += len(body)
            if total > MAX_BUNDLE_BYTES:
                await _send_413(send)
                return
            chunks.append(body)
            more_body = message.get("more_body", False)

        buffered = b"".join(chunks)
        sent = False

        async def _replay() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": buffered, "more_body": False}

        await self.app(scope, _replay, send)


async def _send_413(send: Send) -> None:
    body = (
        b'{"detail":"fixture bundle exceeds max size '
        + str(MAX_BUNDLE_BYTES).encode()
        + b' bytes"}'
    )
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
