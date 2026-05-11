"""Run execution engine.

Pure execution: the engine fires HTTP, evaluates assertions, and
writes the verdict + evidence. It MUST NOT import any ``fastapi.*``
symbol (the route module is the only HTTP-aware layer). The engine
also MUST NOT consult ``X-Twin-*`` overrides — outbound URLs are
exactly what ``requests.url`` resolves to after ``{{VAR}}``
substitution (twin overrides are dev-only, and routing per-request
overrides through the engine would be a security footgun).

Slice-2 runs in-process via FastAPI ``BackgroundTasks``. A uvicorn
restart mid-run leaves the row at ``status='running'`` forever; the
stale-run reaper is slice-3. Idempotency under double-dispatch is
guarded by:

  - ``UPDATE ... WHERE id=$1 AND status='pending'`` on the
    pending→running transition; 0 rows → abort silently.
  - ``UPDATE ... WHERE id=$1 AND status='running'`` on the terminal
    UPDATE; 0 rows → skip the ``run.completed`` event.

Public surface is exactly one coroutine: ``execute_run(run_id)``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any
from uuid import UUID

import httpx

from api.config import get_settings
from api.environments.substitution import load_substitution_map_with_secret_keys
from api.runs._metrics import run_duration_seconds, run_request_count, runs_total
from api.runs._redact import redact_secrets
from api.runs._ssrf import is_blocked_host
from api.runs.schemas import RunEvidence, RunRequestResult
from api.shared.db import get_pool
from api.shared.events import record_event

logger = logging.getLogger(__name__)

_OUTBOUND_TIMEOUT_SECONDS = 10.0
_BODY_EXCERPT_MAX_CHARS = 1000
_TRANSPORT_ERROR_MAX_CHARS = 500
_SUBSTITUTION_TOKEN_RE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")


# ---------- substitution ----------


def _substitute(value: Any, subs: dict[str, str]) -> Any:
    """Walk strings/dicts/lists, replacing ``{{KEY}}`` with subs[KEY].

    Miss policy: leave the placeholder intact rather than raising. The
    engine reports missed keys via ``RunRequestResult.substitution_missed``.
    """
    if isinstance(value, str):
        out = value
        for k, v in subs.items():
            out = out.replace("{{" + k + "}}", str(v))
        return out
    if isinstance(value, dict):
        return {k: _substitute(v, subs) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, subs) for v in value]
    return value


def _find_substitution_misses(payload: Any) -> list[str]:
    """Return the de-duped, order-preserved list of ``{{X}}`` names left over."""
    rendered = json.dumps(payload, default=str)
    seen: list[str] = []
    for match in _SUBSTITUTION_TOKEN_RE.findall(rendered):
        if match not in seen:
            seen.append(match)
    return seen


# ---------- JSONPath-lite ----------


_MISSING = object()


def _resolve_path(body: Any, path: str) -> Any:
    """Tiny JSONPath-lite: ``$.a.b[0]`` style. Returns ``_MISSING`` on miss.

    Bracket-quoted keys (``$['a.b']``) are intentionally unsupported
    — slice-3 if anyone needs them.
    """
    if not isinstance(path, str) or not path:
        return _MISSING
    if path.startswith("$."):
        path = path[2:]
    elif path.startswith("$"):
        path = path[1:]
    cur: Any = body
    token = ""
    i = 0
    while i < len(path):
        c = path[i]
        if c == ".":
            if token:
                if not isinstance(cur, dict) or token not in cur:
                    return _MISSING
                cur = cur[token]
                token = ""
            i += 1
            continue
        if c == "[":
            if token:
                if not isinstance(cur, dict) or token not in cur:
                    return _MISSING
                cur = cur[token]
                token = ""
            close = path.find("]", i)
            if close == -1:
                return _MISSING
            try:
                idx = int(path[i + 1 : close])
            except ValueError:
                return _MISSING
            if not isinstance(cur, list) or idx >= len(cur) or idx < -len(cur):
                return _MISSING
            cur = cur[idx]
            i = close + 1
            continue
        token += c
        i += 1
    if token:
        if not isinstance(cur, dict) or token not in cur:
            return _MISSING
        cur = cur[token]
    return cur


# ---------- evaluation kinds ----------


def _eval_status_eq(observed_status: int, _body, _headers, config: dict) -> tuple[bool, dict]:
    expected = int(config.get("expected"))
    return observed_status == expected, {"observed": observed_status, "expected": expected}


def _eval_json_path_eq(_status, body, _headers, config: dict) -> tuple[bool, dict]:
    path = config["path"]
    expected = config["expected"]
    observed = _resolve_path(body, path)
    if observed is _MISSING:
        return False, {
            "path": path,
            "expected": expected,
            "observed": None,
            "missing": True,
        }
    return observed == expected, {"path": path, "expected": expected, "observed": observed}


def _eval_header_present(_status, _body, headers: dict, config: dict) -> tuple[bool, dict]:
    target = config["header"].lower()
    found = any(k.lower() == target for k in headers.keys())
    return found, {"header": config["header"], "present": found}


def _eval_body_contains(_status, body, _headers, config: dict) -> tuple[bool, dict]:
    needle = config["needle"]
    if isinstance(body, str):
        rendered = body
    else:
        rendered = json.dumps(body) if body is not None else ""
    contains = needle in rendered
    return contains, {"needle": needle, "present": contains}


_KINDS = {
    "status_eq": _eval_status_eq,
    "json_path_eq": _eval_json_path_eq,
    "header_present": _eval_header_present,
    "body_contains": _eval_body_contains,
}


# ---------- header sanitisation ----------


def _sanitise_headers(headers: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Drop header VALUES containing CR/LF/NUL; return ``(safe_headers, dropped_names)``.

    Belt-and-suspenders against CRLF injection through substituted
    secrets. httpx will likely reject too, but failing here keeps the
    error contained and surfaces a clear evidence entry.
    """
    safe: dict[str, str] = {}
    dropped: list[str] = []
    for name, value in headers.items():
        if not isinstance(value, str):
            value = str(value)
        if "\r" in value or "\n" in value or "\x00" in value:
            dropped.append(name)
            continue
        safe[name] = value
    return safe, dropped


# ---------- excerpt + redaction ----------


def _truncate_body_excerpt(body: Any) -> Any:
    if isinstance(body, str):
        return body[:_BODY_EXCERPT_MAX_CHARS]
    return body


# ---------- per-request execution ----------


async def _execute_request(
    client: httpx.AsyncClient,
    request_row: dict[str, Any],
    evaluations: list[dict[str, Any]],
    subs: dict[str, str],
    env_name: str,
) -> tuple[dict[str, Any], Any]:
    """Fire one request, evaluate, return ``(raw_result_dict, raw_response_body)``.

    ``raw_result_dict`` is shaped like ``RunRequestResult`` but is NOT
    yet redacted — the caller redacts in place after harvesting
    captures from ``raw_response_body``. This ordering matters: capture
    paths run against the unredacted response body, and the redaction
    pass then runs over the persisted fields before validation.
    """
    method = request_row["method"]

    raw_url = request_row["url"]
    raw_headers = request_row["headers"] or {}
    if isinstance(raw_headers, str):
        raw_headers = json.loads(raw_headers)
    raw_body = request_row["body"]
    if isinstance(raw_body, str):
        try:
            raw_body = json.loads(raw_body)
        except json.JSONDecodeError:
            pass

    rendered_url = _substitute(raw_url, subs)
    rendered_headers_raw = _substitute(raw_headers, subs)
    rendered_body = _substitute(raw_body, subs)

    string_headers = {str(k): str(v) for k, v in rendered_headers_raw.items()}
    safe_headers, dropped_header_names = _sanitise_headers(string_headers)

    substitution_missed = _find_substitution_misses({
        "url": rendered_url,
        "headers": rendered_headers_raw,
        "body": rendered_body,
    })

    response_status = 0
    response_headers: dict[str, str] = {}
    response_body: Any = None
    transport_error: str | None = None
    started = time.monotonic()

    blocked, reason = is_blocked_host(rendered_url, env_name)
    if blocked:
        transport_error = f"blocked: SSRF guard rejected {reason}"
        logger.warning("run engine: SSRF block: %s", reason)
    elif dropped_header_names:
        transport_error = (
            "blocked: header sanitiser dropped CR/LF in "
            + ",".join(sorted(dropped_header_names))
        )
        logger.warning(
            "run engine: header sanitiser dropped %d header(s)",
            len(dropped_header_names),
        )
    else:
        try:
            json_body = rendered_body
            r = await client.request(
                method=method,
                url=rendered_url,
                headers=safe_headers,
                json=json_body if (json_body is not None and not isinstance(json_body, str)) else None,
                content=json_body if isinstance(json_body, str) else None,
                timeout=_OUTBOUND_TIMEOUT_SECONDS,
            )
            response_status = r.status_code
            response_headers = dict(r.headers)
            ct = r.headers.get("content-type", "")
            if "json" in ct.lower():
                try:
                    response_body = r.json()
                except Exception:  # noqa: BLE001
                    response_body = r.text
            else:
                response_body = r.text
        except Exception as e:  # noqa: BLE001
            # Truncate defensively — httpx exception strings can carry
            # the full post-substitution URL (with secrets) and DNS
            # error messages can be adversarially long.
            transport_error = (f"{type(e).__name__}: {e}")[:_TRANSPORT_ERROR_MAX_CHARS]

    elapsed_ms = int((time.monotonic() - started) * 1000)

    eval_results: list[dict[str, Any]] = []
    if transport_error is None:
        for ev in evaluations:
            kind = ev["kind"]
            config = ev["config"] or {}
            if isinstance(config, str):
                config = json.loads(config)
            fn = _KINDS.get(kind)
            if fn is None:
                eval_results.append({
                    "id": str(ev["id"]),
                    "name": ev["name"],
                    "kind": kind,
                    "passed": False,
                    "detail": {},
                    "error": "unknown kind",
                })
                continue
            try:
                passed, detail = fn(
                    response_status, response_body, response_headers, config,
                )
                eval_results.append({
                    "id": str(ev["id"]),
                    "name": ev["name"],
                    "kind": kind,
                    "passed": passed,
                    "detail": detail,
                    "error": None,
                })
            except Exception as e:  # noqa: BLE001
                eval_results.append({
                    "id": str(ev["id"]),
                    "name": ev["name"],
                    "kind": kind,
                    "passed": False,
                    "detail": {},
                    "error": f"{type(e).__name__}: {e}",
                })

    body_excerpt = _truncate_body_excerpt(response_body)

    result: dict[str, Any] = {
        "request_id": str(request_row["id"]),
        "request_name": request_row["name"],
        "method": method,
        "url": rendered_url,
        "response_status": response_status,
        "response_headers": response_headers,
        "response_body_excerpt": body_excerpt,
        "latency_ms": elapsed_ms,
        "transport_error": transport_error,
        "substitution_missed": substitution_missed,
        "evaluations": eval_results,
    }
    return result, response_body


def _apply_captures(
    response_body: Any,
    capture_spec: dict[str, str],
    subs: dict[str, str],
) -> dict[str, str]:
    """Resolve each capture path; mutate ``subs``; return ``captured_dict``.

    Captured values overlay env vars (last-write-wins) so downstream
    requests see them. Caller persists only the KEYS — values are
    never written to JSONB.
    """
    captured: dict[str, str] = {}
    if not capture_spec:
        return captured
    for name, path in capture_spec.items():
        value = _resolve_path(response_body, path)
        if value is _MISSING:
            continue
        coerced = value if isinstance(value, str) else json.dumps(value, default=str)
        subs[name] = coerced
        captured[name] = coerced
    return captured


def _redact_result_in_place(result: dict[str, Any], secret_values: set[str]) -> None:
    """Apply secret-value redaction to url + header values + body excerpt + transport_error."""
    if not secret_values:
        return
    result["url"] = redact_secrets(secret_values, result["url"])
    result["response_headers"] = {
        k: redact_secrets(secret_values, v) for k, v in result["response_headers"].items()
    }
    result["response_body_excerpt"] = redact_secrets(
        secret_values, result["response_body_excerpt"]
    )
    # httpx exception strings can echo the full URL (which has been
    # substituted with secret values). Redact those before persistence.
    if result.get("transport_error") is not None:
        result["transport_error"] = redact_secrets(secret_values, result["transport_error"])


# ---------- run aggregation ----------


def _aggregate_verdict(request_results: list[RunRequestResult]) -> tuple[str, str]:
    """Return ``(status, verdict)`` per the spec's aggregation rules."""
    if not request_results:
        return "error", "error"
    if any(r.transport_error for r in request_results):
        return "error", "error"
    if not any(r.evaluations for r in request_results):
        return "error", "error"
    if all(r.passed for r in request_results):
        return "passed", "passed"
    return "failed", "failed"


# ---------- entry point ----------


async def execute_run(run_id: UUID) -> None:
    """Background entry point. Loads run state, executes, writes verdict."""
    settings = get_settings()
    pool = get_pool()

    # ---- Acquire #1: pending→running guarded transition + batched reads.
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT collection_id, environment_id FROM runs WHERE id = $1",
                run_id,
            )
            if row is None:
                logger.error("execute_run: run %s not found", run_id)
                return

            started_row = await conn.fetchrow(
                """
                UPDATE runs
                SET status = 'running', started_at = now()
                WHERE id = $1 AND status = 'pending'
                RETURNING id
                """,
                run_id,
            )
            if started_row is None:
                logger.info(
                    "execute_run: run %s no longer pending; aborting re-fire",
                    run_id,
                )
                return

            await record_event(
                conn,
                actor_kind="system",
                actor_id=None,
                kind="run.started",
                target_kind="run",
                target_id=run_id,
                payload={},
            )

            subs: dict[str, str] = {}
            secret_values: set[str] = set()
            if row["environment_id"] is not None:
                merged, secret_keys = await load_substitution_map_with_secret_keys(
                    conn, row["environment_id"]
                )
                subs = dict(merged)
                secret_values = {merged[k] for k in secret_keys if merged.get(k)}

            request_rows: list[dict[str, Any]] = []
            eval_rows_by_request: dict[str, list[dict[str, Any]]] = {}
            if row["collection_id"] is not None:
                items = await conn.fetch(
                    """
                    SELECT
                        ci.position,
                        r.id           AS request_id,
                        r.name,
                        r.method,
                        r.url,
                        r.headers,
                        r.body,
                        r.capture
                    FROM collection_items ci
                    JOIN requests r ON r.id = ci.request_id
                    WHERE ci.collection_id = $1
                    ORDER BY ci.position ASC
                    """,
                    row["collection_id"],
                )
                request_rows = [dict(it) for it in items]
                if request_rows:
                    request_ids = [r["request_id"] for r in request_rows]
                    evals = await conn.fetch(
                        """
                        SELECT id, request_id, name, kind, config
                        FROM evaluations
                        WHERE request_id = ANY($1::uuid[])
                        ORDER BY request_id, created_at ASC
                        """,
                        request_ids,
                    )
                    for ev in evals:
                        key = str(ev["request_id"])
                        eval_rows_by_request.setdefault(key, []).append(dict(ev))
    except Exception:  # noqa: BLE001
        logger.exception("execute_run: pre-flight failed for run %s", run_id)
        await _record_terminal_error(run_id, [], engine_error="pre-flight failed")
        return

    # ---- HTTP loop: no DB connection held.
    typed_results: list[RunRequestResult] = []
    try:
        async with httpx.AsyncClient() as client:
            for req_row in request_rows:
                capture_spec = req_row.get("capture") or {}
                if isinstance(capture_spec, str):
                    capture_spec = json.loads(capture_spec)

                ev_rows = eval_rows_by_request.get(str(req_row["request_id"]), [])
                ev_rows_normalised = [
                    {
                        "id": ev["id"],
                        "name": ev["name"],
                        "kind": ev["kind"],
                        "config": (
                            json.loads(ev["config"])
                            if isinstance(ev["config"], str)
                            else (ev["config"] or {})
                        ),
                    }
                    for ev in ev_rows
                ]

                # _execute_request returns (raw_dict, raw_body). The
                # raw_body feeds captures; the raw_dict is then
                # redacted in place; THEN we compute `captured` keys
                # and `passed`; THEN model_validate.
                proxy_row = {
                    "id": req_row["request_id"],
                    "name": req_row["name"],
                    "method": req_row["method"],
                    "url": req_row["url"],
                    "headers": req_row["headers"],
                    "body": req_row["body"],
                }
                result_dict, raw_response_body = await _execute_request(
                    client,
                    proxy_row,
                    ev_rows_normalised,
                    subs,
                    settings.env,
                )

                # (b) redaction pass: url + header values + body excerpt.
                _redact_result_in_place(result_dict, secret_values)

                # (c) captures: derived from the *raw* response body so
                # we can chain values forward. We never persist the values.
                captured = _apply_captures(raw_response_body, capture_spec, subs)
                result_dict["captured"] = sorted(captured.keys())

                # Per-request `passed`.
                evals_out = result_dict.get("evaluations", [])
                result_dict["passed"] = (
                    result_dict["transport_error"] is None
                    and len(evals_out) > 0
                    and all(ev.get("passed", False) for ev in evals_out)
                )

                # (d) validate AFTER redaction + captures + passed.
                typed_results.append(RunRequestResult.model_validate(result_dict))
    except Exception:  # noqa: BLE001
        logger.exception("execute_run: HTTP loop crashed for run %s", run_id)
        await _record_terminal_error(
            run_id, typed_results, engine_error="http loop crashed"
        )
        return

    status, verdict = _aggregate_verdict(typed_results)
    evidence = RunEvidence(requests=typed_results)

    # ---- Acquire #2: terminal UPDATE + completed event.
    try:
        async with pool.acquire() as conn:
            updated = await conn.fetchrow(
                """
                UPDATE runs
                SET status = $1, verdict = $2, evidence = $3::jsonb, finished_at = now()
                WHERE id = $4 AND status = 'running'
                RETURNING started_at, finished_at
                """,
                status,
                verdict,
                json.dumps(evidence.model_dump(mode="json")),
                run_id,
            )
            if updated is None:
                logger.info(
                    "execute_run: terminal UPDATE matched 0 rows for run %s; skipping completion event",
                    run_id,
                )
                return

            duration_ms = 0
            duration_seconds = 0.0
            if updated["started_at"] is not None and updated["finished_at"] is not None:
                delta = (updated["finished_at"] - updated["started_at"]).total_seconds()
                duration_seconds = max(0.0, delta)
                duration_ms = int(duration_seconds * 1000)
            # Metrics — emitted only after the UPDATE matched a row, so a
            # losing-the-race second dispatch never double-counts.
            runs_total.labels(verdict=verdict).inc()
            run_duration_seconds.labels(verdict=verdict).observe(duration_seconds)
            run_request_count.observe(len(typed_results))
            await record_event(
                conn,
                actor_kind="system",
                actor_id=None,
                kind="run.completed",
                target_kind="run",
                target_id=run_id,
                payload={
                    "verdict": verdict,
                    "status": status,
                    "request_count": len(typed_results),
                    "duration_ms": duration_ms,
                },
            )
        logger.info(
            "run completed run_id=%s verdict=%s status=%s requests=%d duration_ms=%d",
            run_id,
            verdict,
            status,
            len(typed_results),
            duration_ms,
        )
    except Exception:  # noqa: BLE001
        logger.exception("execute_run: terminal write failed for run %s", run_id)


async def _record_terminal_error(
    run_id: UUID,
    request_results: list[RunRequestResult],
    *,
    engine_error: str,
) -> None:
    """Best-effort terminal-error write when the engine itself bombs out."""
    try:
        evidence = RunEvidence(requests=request_results, engine_error=engine_error)
        evidence_json = evidence.model_dump(mode="json")
        pool = get_pool()
        async with pool.acquire() as conn:
            updated = await conn.fetchrow(
                """
                UPDATE runs
                SET status = 'error', verdict = 'error', evidence = $1::jsonb,
                    finished_at = now()
                WHERE id = $2 AND status = 'running'
                RETURNING id
                """,
                json.dumps(evidence_json),
                run_id,
            )
            if updated is None:
                logger.error(
                    "execute_run: best-effort terminal-error UPDATE matched 0 rows for run %s",
                    run_id,
                )
                return
            # Engine-error path counts as 'error' verdict for metrics.
            runs_total.labels(verdict="error").inc()
            run_request_count.observe(len(request_results))
            await record_event(
                conn,
                actor_kind="system",
                actor_id=None,
                kind="run.completed",
                target_kind="run",
                target_id=run_id,
                payload={
                    "verdict": "error",
                    "status": "error",
                    "request_count": len(request_results),
                    "engine_error": engine_error,
                },
            )
    except Exception:  # noqa: BLE001
        logger.exception(
            "execute_run: best-effort terminal-error write itself failed for run %s",
            run_id,
        )
