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

import asyncio
import json
import logging
import re
import time
from typing import Any
from uuid import UUID

import httpx

from api.config import get_settings
from api.environments.substitution import load_substitution_map_with_secret_keys
from api.runs._constants import RUN_BUDGET_SECONDS
from api.runs._metrics import run_duration_seconds, run_request_count, runs_total
from api.runs._redact import redact_secrets
from api.runs._ssrf import is_blocked_host
from api.runs.schemas import RunEvidence, RunRequestResult
from api.shared.db import get_pool
from api.shared.events import record_event

logger = logging.getLogger(__name__)

_OUTBOUND_TIMEOUT_SECONDS = 10.0
_CONNECT_TIMEOUT_CAP_SECONDS = 10.0
# httpx's read timeout is per chunk, so a target that trickles bytes faster
# than the read timeout never times out. Each attempt is additionally
# wrapped in ``asyncio.timeout(timeout + this)`` so a single attempt has a
# hard ceiling.
_ATTEMPT_CEILING_EXTRA_SECONDS = 10.0
_BODY_EXCERPT_MAX_CHARS = 1000
_TRANSPORT_ERROR_MAX_CHARS = 500
# Transient retry, the platform runner's rule: an attempt is fired and
# evaluated; it is re-fired only when it is not passing and the cause is a
# transport error (timeouts included) or one of these statuses.
_TRANSIENT_STATUSES = frozenset({429, 502, 503, 504})
_TRANSIENT_MAX_ATTEMPTS = 4
_TRANSIENT_BASE_DELAY_MS = 250
# Deterministic transport failures that must not be re-fired: an unrendered
# ``{{BASE_URL}}`` (no scheme) and a header the sanitiser let through but
# h11 refuses.
_NON_TRANSIENT_TRANSPORT_ERRORS = (httpx.UnsupportedProtocol, httpx.LocalProtocolError)
_FILTER_RE = re.compile(r"\[\?\(@\.([^.\[\]=\s]+)==(?:'([^']*)'|\"([^\"]*)\")\)\]")
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
            # Filter form: first object element whose key is a string equal
            # to the literal. Matched before the integer-index path so a
            # ']' inside the quoted literal cannot truncate the match.
            m = _FILTER_RE.match(path, i)
            if m is not None:
                key = m.group(1)
                lit = m.group(2) if m.group(2) is not None else m.group(3)
                if not isinstance(cur, list):
                    return _MISSING
                hit = next(
                    (
                        el
                        for el in cur
                        if isinstance(el, dict)
                        and isinstance(el.get(key), str)
                        and el[key] == lit
                    ),
                    _MISSING,
                )
                if hit is _MISSING:
                    return _MISSING
                cur = hit
                i = m.end()
                continue
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
    detail: dict[str, Any] = {"path": path, "expected": expected}
    if "expected_raw" in config:
        detail["expected_raw"] = config["expected_raw"]
    if observed is _MISSING:
        detail.update({"observed": None, "missing": True})
        return False, detail
    detail["observed"] = observed
    return observed == expected, detail


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
    # A needle that rendered to "" would match vacuously; treat it as failed.
    contains = bool(needle) and needle in rendered
    detail: dict[str, Any] = {"needle": needle, "present": contains}
    if not needle:
        detail["empty_needle"] = True
    if "needle_raw" in config:
        detail["needle_raw"] = config["needle_raw"]
    return contains, detail


def _json_type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _is_json_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _eval_json_path_exists(_status, body, _headers, config: dict) -> tuple[bool, dict]:
    path = config["path"]
    observed = _resolve_path(body, path)
    missing = observed is _MISSING
    return (not missing), {
        "path": path,
        "missing": missing,
        "observed_type": None if missing else _json_type_name(observed),
    }


def _eval_json_path_not_exists(_status, body, _headers, config: dict) -> tuple[bool, dict]:
    path = config["path"]
    observed = _resolve_path(body, path)
    missing = observed is _MISSING
    return missing, {
        "path": path,
        "missing": missing,
        "observed_type": None if missing else _json_type_name(observed),
    }


def _eval_json_path_contains(_status, body, _headers, config: dict) -> tuple[bool, dict]:
    path = config["path"]
    needle = config["needle"]
    observed = _resolve_path(body, path)
    detail: dict[str, Any] = {
        "path": path,
        "needle": needle,
        "needle_raw": config.get("needle_raw", needle),
        "missing": observed is _MISSING,
        "found": False,
        "observed_type": None,
        "observed": None,
    }
    if observed is _MISSING:
        return False, detail
    detail["observed_type"] = _json_type_name(observed)
    if not isinstance(observed, str):
        return False, detail
    if not needle:
        detail["empty_needle"] = True
        return False, detail
    detail["found"] = needle in observed
    # Bounded by the caller's redact-then-bound pass.
    detail["observed"] = observed
    return detail["found"], detail


_CMP_OPS = {
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
}


def _eval_json_path_cmp(_status, body, _headers, config: dict) -> tuple[bool, dict]:
    path = config["path"]
    op = config["op"]
    expected = config["expected"]
    observed = _resolve_path(body, path)
    detail: dict[str, Any] = {
        "path": path,
        "op": op,
        "expected": expected,
        "missing": observed is _MISSING,
        "observed_type": None,
        "observed": None,
    }
    if observed is _MISSING:
        return False, detail
    detail["observed_type"] = _json_type_name(observed)
    if not _is_json_number(observed):
        return False, detail
    detail["observed"] = observed
    return bool(_CMP_OPS[op](observed, expected)), detail


_KINDS = {
    "status_eq": _eval_status_eq,
    "json_path_eq": _eval_json_path_eq,
    "header_present": _eval_header_present,
    "body_contains": _eval_body_contains,
    "json_path_exists": _eval_json_path_exists,
    "json_path_not_exists": _eval_json_path_not_exists,
    "json_path_contains": _eval_json_path_contains,
    "json_path_cmp": _eval_json_path_cmp,
}

# Config fields rendered through {{VAR}} substitution at evaluation time,
# by name: never ``path``. Strings only, no coercion.
_RENDERED_CONFIG_FIELDS: dict[str, tuple[str, ...]] = {
    "json_path_eq": ("expected",),
    "json_path_contains": ("needle",),
    "body_contains": ("needle",),
}


def _render_config(kind: str, config: dict, subs: dict[str, str]) -> tuple[dict, list[str]]:
    """Return ``(rendered_config, missed_names)``; raw values kept as ``<field>_raw``."""
    fields = _RENDERED_CONFIG_FIELDS.get(kind)
    if not fields:
        return config, []
    out = dict(config)
    missed: list[str] = []
    for field in fields:
        raw = config.get(field)
        if isinstance(raw, str):
            rendered = _substitute(raw, subs)
            out[field] = rendered
            out[f"{field}_raw"] = raw
            missed.extend(_find_substitution_misses(rendered))
    return out, missed


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
    timeout_s: float = _OUTBOUND_TIMEOUT_SECONDS,
    timeout_ms: int | None = None,
) -> tuple[dict[str, Any], Any, bool, bool]:
    """Fire one request, evaluate, return ``(raw_result_dict, raw_response_body, transient, fired)``.

    ``raw_result_dict`` is shaped like ``RunRequestResult`` but is NOT yet
    redacted or bounded: the caller runs one redact-then-bound pass over
    it (excerpt, error string, evaluation details) before persistence.
    ``transient`` is decided where the exception is caught, never derived
    from the error string; ``fired`` says whether an HTTP attempt went out.
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
    transient = False
    fired = False
    is_timeout = False
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
        fired = True
        try:
            json_body = rendered_body
            async with asyncio.timeout(timeout_s + _ATTEMPT_CEILING_EXTRA_SECONDS):
                r = await client.request(
                    method=method,
                    url=rendered_url,
                    headers=safe_headers,
                    json=json_body
                    if (json_body is not None and not isinstance(json_body, str))
                    else None,
                    content=json_body if isinstance(json_body, str) else None,
                    timeout=httpx.Timeout(
                        timeout_s, connect=min(timeout_s, _CONNECT_TIMEOUT_CAP_SECONDS)
                    ),
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
        except TimeoutError:
            # The per-attempt ceiling: a trickling response that never
            # trips httpx's per-chunk read timeout.
            transport_error = "TimeoutError: attempt ceiling"
            transient = True
            is_timeout = True
        except _NON_TRANSIENT_TRANSPORT_ERRORS as e:
            transport_error = f"{type(e).__name__}: {e}"
        except httpx.TransportError as e:
            # All four timeout classes, network, protocol and proxy errors.
            transport_error = f"{type(e).__name__}: {e}".rstrip(": ")
            transient = True
            is_timeout = isinstance(e, httpx.TimeoutException)
        except Exception as e:  # noqa: BLE001
            transport_error = f"{type(e).__name__}: {e}"

    elapsed_ms = int((time.monotonic() - started) * 1000)

    eval_results: list[dict[str, Any]] = []
    if transport_error is None:
        for ev in evaluations:
            kind = ev["kind"]
            config = ev["config"] or {}
            if isinstance(config, str):
                config = json.loads(config)
            config, config_missed = _render_config(kind, config, subs)
            for name in config_missed:
                if name not in substitution_missed:
                    substitution_missed.append(name)
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

    # Excerpt and error string are bounded by the caller AFTER redaction.
    body_excerpt = response_body
    effective_timeout_ms = timeout_ms if timeout_ms else int(timeout_s * 1000)
    error_suffix = f" (timeout_ms={effective_timeout_ms})" if is_timeout else ""
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
        "_error_suffix": error_suffix,
    }
    return result, response_body, transient, fired


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
    """One redact-then-bound pass over everything persisted from a result.

    Redaction first, then the size bounds, so a secret straddling a cut
    cannot leave its prefix behind. Covers url, header values, the body
    excerpt, the transport error string, and every evaluation detail.
    """
    if secret_values:
        result["url"] = redact_secrets(secret_values, result["url"])
        result["response_headers"] = {
            k: redact_secrets(secret_values, v) for k, v in result["response_headers"].items()
        }
        result["response_body_excerpt"] = redact_secrets(
            secret_values, result["response_body_excerpt"]
        )
        if result.get("transport_error") is not None:
            result["transport_error"] = redact_secrets(secret_values, result["transport_error"])
        for ev in result.get("evaluations", []):
            if isinstance(ev.get("detail"), dict):
                ev["detail"] = redact_secrets(secret_values, ev["detail"])
    # Bounds, per field: excerpt 1,000 chars (strings only), error string
    # 500 chars with any timeout suffix appended after the cut,
    # json_path_contains.observed 1,000 chars.
    result["response_body_excerpt"] = _truncate_body_excerpt(result["response_body_excerpt"])
    suffix = result.pop("_error_suffix", "") or ""
    if result.get("transport_error") is not None:
        base = result["transport_error"][: max(0, _TRANSPORT_ERROR_MAX_CHARS - len(suffix))]
        result["transport_error"] = base + suffix
    for ev in result.get("evaluations", []):
        detail = ev.get("detail")
        if ev.get("kind") == "json_path_contains" and isinstance(detail, dict):
            if isinstance(detail.get("observed"), str):
                detail["observed"] = detail["observed"][:_BODY_EXCERPT_MAX_CHARS]


def _result_passed(result: dict[str, Any]) -> bool:
    evals_out = result.get("evaluations", [])
    return (
        result.get("transport_error") is None
        and len(evals_out) > 0
        and all(ev.get("passed", False) for ev in evals_out)
    )


def _skipped_result(req_row: dict[str, Any], subs: dict[str, str]) -> dict[str, Any]:
    """Record for an item the run budget never let start; mirrors ``blocked:``."""
    return {
        "request_id": str(req_row["request_id"]),
        "request_name": req_row["name"],
        "method": req_row["method"],
        "url": _substitute(req_row["url"], subs),
        "response_status": 0,
        "response_headers": {},
        "response_body_excerpt": None,
        "latency_ms": 0,
        "transport_error": "skipped: run budget exceeded",
        "substitution_missed": [],
        "evaluations": [],
        "captured": [],
        "passed": False,
        "attempts": 0,
    }


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
            # Run builtins: merged after environment variables (a reserved
            # name is rejected at every write path, so nothing is
            # overridden in practice) and before captures.
            subs["RUN_ID"] = run_id.hex[:12]

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
                        r.capture,
                        r.timeout_ms
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
    budget_started = time.monotonic()
    budget_tripped = False

    def _budget_exceeded() -> bool:
        nonlocal budget_tripped
        if time.monotonic() - budget_started > RUN_BUDGET_SECONDS:
            budget_tripped = True
            return True
        return False

    try:
        async with httpx.AsyncClient() as client:
            for req_row in request_rows:
                if _budget_exceeded():
                    skipped = _skipped_result(req_row, subs)
                    _redact_result_in_place(skipped, secret_values)
                    typed_results.append(RunRequestResult.model_validate(skipped))
                    continue
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

                # _execute_request returns (raw_dict, raw_body, transient, fired). The
                # raw_body feeds captures; the raw_dict is then
                # redacted in place; THEN we compute `captured` keys
                # and `passed`; THEN model_validate.
                timeout_ms = req_row.get("timeout_ms")
                timeout_s = (timeout_ms / 1000.0) if timeout_ms else _OUTBOUND_TIMEOUT_SECONDS
                proxy_row = {
                    "id": req_row["request_id"],
                    "name": req_row["name"],
                    "method": req_row["method"],
                    "url": req_row["url"],
                    "headers": req_row["headers"],
                    "body": req_row["body"],
                    "timeout_ms": timeout_ms,
                }
                attempts = 0
                while True:
                    attempts += 1
                    result_dict, raw_response_body, transient, fired = await _execute_request(
                        client,
                        proxy_row,
                        ev_rows_normalised,
                        subs,
                        settings.env,
                        timeout_s,
                        timeout_ms,
                    )
                    passed_now = _result_passed(result_dict)
                    retryable = (not passed_now) and (
                        transient
                        or (
                            result_dict["transport_error"] is None
                            and result_dict["response_status"] in _TRANSIENT_STATUSES
                        )
                    )
                    if (
                        retryable
                        and attempts < _TRANSIENT_MAX_ATTEMPTS
                        and not _budget_exceeded()
                    ):
                        await asyncio.sleep(
                            _TRANSIENT_BASE_DELAY_MS / 1000.0 * (2 ** (attempts - 1))
                        )
                        continue
                    break
                result_dict["attempts"] = attempts if fired else 0
                # Redact-then-bound pass over everything persisted.
                _redact_result_in_place(result_dict, secret_values)
                # Captures come from the raw body so values chain forward;
                # only their keys are persisted.
                captured = _apply_captures(raw_response_body, capture_spec, subs)
                result_dict["captured"] = sorted(captured.keys())
                result_dict["passed"] = _result_passed(result_dict)
                typed_results.append(RunRequestResult.model_validate(result_dict))
    except Exception:  # noqa: BLE001
        logger.exception("execute_run: HTTP loop crashed for run %s", run_id)
        await _record_terminal_error(
            run_id, typed_results, engine_error="http loop crashed"
        )
        return

    status, verdict = _aggregate_verdict(typed_results)
    if budget_tripped:
        status, verdict = "error", "error"
    evidence = RunEvidence(
        requests=typed_results,
        engine_error="run budget exceeded" if budget_tripped else None,
    )

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
