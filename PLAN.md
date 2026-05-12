# PLAN

> Build: pagehub-evals JTBD pivot — finish the `runs/` verdict-bearing engine. Read `SPEC.md` first.

## Security

### Threat model

Actors and capabilities specific to a verdict gate that fires outbound HTTP from a server-side worker against URLs supplied via the DB:

- **Harness-key holder** (untrusted-ish; one per installation): can POST `/v1/runs` with a chosen `collection_id`/`environment_id`/`harness_claim`. CANNOT author `requests` rows (`api/requests/routes.py:48,77,93` are `require_user`), CANNOT see other actors' runs (`routes.py:137-138`). Threat surface: choosing which (operator-authored) collection runs, reading own-run `evidence`.
- **Compromised operator JWT or admin** (trusted but key-loss is a real failure mode): can author arbitrary `requests.url`, `requests.headers`, `requests.body`. Anything an operator can do, a stolen operator token can do. SSRF risk is bounded by *what the operator was already allowed to do* — but a stolen token from a low-privilege operator should not become a metadata-endpoint read primitive against the platform's egress.
- **Target server under test** (adversarial response): chooses response status, headers, and body. Can echo back substituted secret material (`Authorization: Bearer …`) in 4xx body, can return giant payloads, can return crafted headers that make their way into our `evidence` JSONB and back out via `GET /v1/runs/{id}`.
- **Unauthenticated caller**: blocked at `require_auth` / `require_user` everywhere (`dependencies.py:112-129`). No path on runs accepts anonymous traffic.

Top concrete threats:

1. **SSRF via `requests.url`** — engine fires from the platform's egress network. Operator-authored URL → metadata IP, internal Supabase, sibling Vercel functions, etc. Outbound is mandatory (we are by design a black-box checker), so the mitigation is a deny-list, not an allow-list.
2. **Secret leakage via `evidence`** — `load_substitution_map` decrypts secrets into a Python dict (`environments/routes.py:188-202`). Engine substitutes them into outbound url/headers/body, fires, then stores `response_body_excerpt` (up to 1000 chars) and `response_headers` verbatim. An adversarial target echoes `Bearer XYZ` back in a 4xx body or a `Set-Cookie`; the secret lands in `evidence` JSONB and is returned by `GET /v1/runs/{id}`.
3. **Substituted secrets re-stored in `evidence.requests[*].url`/`headers`** — even with a benign target, the *outgoing* `Authorization: Bearer <decrypted>` header is currently echoed into evidence verbatim (`engine.py:174` then `engine.py:236-240` writes `response_headers`, but the outgoing headers are NOT currently captured into evidence — confirm; the larger risk is the response side echo and slice-3 may add request-side mirroring, so codify the rule now).
4. **Substitution miss leaks template into target** — `{{API_KEY}}` left literal in a URL transmits the string `{{API_KEY}}` to a third-party host. Low severity (no real secret leaks) but a tell about our internals.
5. **Cross-actor read scope** — harness key reading another harness's runs. Clamped at `routes.py:137-138`; eval seed must exercise the 403.
6. **PATCH bypass / verdict re-write via raw asyncpg** — engine writes verdict via direct `UPDATE` (`engine.py:334-344`). Any future code path that issues `UPDATE runs SET verdict=…` re-introduces the bypass. Slice-2 invariant: engine.py is the SOLE writer of `verdict`, `status` (post-INSERT), `evidence`, `started_at`, `finished_at`. `harness_claim`, `created_by_*` are write-once at INSERT (`routes.py:64-83`); no UPDATE path touches them.
7. **JWT slug confusion** — `app_slug` check at `dependencies.py:73-74` is the only thing keeping a `pagehub` or `app-prayers` JWT from being honored here. New code paths that decode JWTs must route through `_verify_jwt` + slug check. Engine does not issue or verify JWTs — leave it that way.
8. **Header injection via substituted values** — `{{TOKEN}}` substituted into a header value containing `\r\n` could split the outbound request (CRLF injection). httpx generally rejects control chars in header values, but the engine should sanitize defensively before handing to `client.request(...)`.

### Authn / Authz (per endpoint)

| Endpoint | Dep | Operator | Harness key | Notes |
|---|---|---|---|---|
| `POST /v1/runs` | `require_auth` | 202 | 202 | Body is `CreateRunRequest`; **add `model_config = ConfigDict(extra="forbid")`** so `verdict`/`status`/`evidence`/`started_at`/`finished_at`/`created_by_*` in the body → 422 at parse time (already architect-mandated, codifying it as a security requirement). Allowed fields: `collection_id`, `environment_id`, `harness_id`, `harness_claim`. |
| `GET /v1/runs` | `require_user` | 200 | **403** | Harness keys blocked by `require_user` (`dependencies.py:139-140`). Eval seed must assert harness key → 403 here. |
| `GET /v1/runs/{id}` | `require_auth` | 200 (any run) | 200 (own runs) / **403** (others) | Clamp at `routes.py:137-138`. Mismatched key returns 403, not 404, so seeds can distinguish "not yours" from "doesn't exist" (designer treats 403==404 in UI; backend stays explicit). |
| `PATCH /v1/runs/{id}` | `require_auth` | **403** | **403** | Unconditional. No body parsing; route exists to make immutability testable. Eval seed asserts BOTH auth kinds receive 403. |

`require_auth` rejects "both headers present" with 422 (`dependencies.py:117-121`) — no actor ambiguity. Empty/revoked/unknown harness keys → 401 (`dependencies.py:94-108`).

### Data exposure

- **`evidence.requests[*].response_body_excerpt`** — verbatim, truncated to 1000 chars (`engine.py:222-229`). Trade: operators are trusted, harnesses see only their own runs. Acceptable. **Mitigation:** see header masking below — if the target echoed a known secret in the body, mask it before persisting.
- **`evidence.requests[*].response_headers`** — verbatim dict from `r.headers` (`engine.py:174`). An adversarial target can put any string into `Set-Cookie`, `WWW-Authenticate`, or a custom header — including a secret it learned from our outgoing request (e.g., the target's own DB error containing `Bearer XYZ`). **REQUIRED: header-value masking pass before storing in evidence.** Build a value-set from the run's substitution map's *secret* values (NOT variable values), and for every response header value, if any secret value appears as a substring, replace it with `***`. Same pass on `response_body_excerpt` (after truncation). Variables are intentionally not masked — they are non-sensitive by definition.
- **Outgoing url/headers/body** — slice-2 does NOT mirror outgoing request meta into evidence (engine writes only `method`, `url` post-substitution, and `request_id`/`request_name` per `engine.py:231-244`). The substituted `url` CAN contain secrets if the operator authored `https://api.example.com/?token={{API_KEY}}`. **DECISION: mask secret-values out of the persisted `url` too**, using the same value-set scan. Operators authoring tokens in querystrings is bad practice but is not the engine's problem to refuse; redacting on persist is.
- **`evidence.requests[*].captured`** (slice-2 add) — keys captured from response bodies (e.g., `AUTH_TOKEN: <jwt>`). Captured values often ARE secret (they came from a login response). **DECISION: keys-only enforced by SCHEMA TYPE** — `RunRequestResult.captured: list[str]` (reconciled from the initial `dict[str, str]` shape). Engine builds a local `captured_dict: dict[str, str]` for the in-process `subs` map (downstream requests still substitute real values), then writes `sorted(captured_dict.keys())` into the persisted form. No redaction pass over `captured` is needed — the type makes value-leakage structurally impossible. The designer spec already shows keys-only in the UI (`SPEC.md:232-233`).
- **`harness_claim`** — stored verbatim up to 10 000 chars. Operators and the originating harness see it. Acceptable; agents are instructed to summarize, not paste transcripts.
- **`actor_id` / `created_by_id`** — UUIDs of harness keys are not themselves secret; the secret is `X-Harness-Key` (hashed in DB via `hash_secret`, `dependencies.py:96`). Listing operators that created runs is fine.
- **Logs** — `engine.py:256` logs `run not found`. Otherwise the engine does not log url/headers/body. **REQUIRED**: keep it that way. No `logger.info(rendered_url)`, no `logger.debug(headers)` without an explicit env gate. httpx itself logs at DEBUG to its own logger; ensure `httpx` logger is at WARNING+ in production (set in logging config, not in engine code).

### SSRF mitigation

Pagehub-evals is intentionally a black-box checker — outbound to arbitrary public hosts is the product. But link-local / metadata / RFC1918 / loopback are not legitimate targets in staging or production. The twin-override pattern is the dev escape hatch (it points to `http://twins:8000/...` which IS RFC1918 in development).

**Required helper, called from `_execute_request` before `client.request(...)`:**

```python
# api/runs/_ssrf.py  (new)
def is_blocked_host(url: str, env: str) -> tuple[bool, str | None]:
    """Return (blocked, reason). In development, never blocks (twin override)."""
```

- In `env in {"staging", "production"}`: resolve the URL's hostname (literal IPs and DNS names), block if the resolved address falls in any of:
  - `127.0.0.0/8`, `169.254.0.0/16`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `0.0.0.0/8`
  - `::1`, `fc00::/7`, `fe80::/10`, `::ffff:0:0/96` (IPv4-mapped IPv6, re-check the embedded v4)
  - Also block schemes other than `http`/`https` (no `file://`, `gopher://`, etc.).
- In `env == "development"`: allow everything. Matches the `env == "development"` gate the twin middleware already enforces (`shared/twin_middleware.py`); new envs are deny-by-default per platform rule.
- On block: short-circuit `_execute_request` to return a result with `response_status=0`, `transport_error="blocked: SSRF guard rejected <reason>"`, no evaluations run. Run-level verdict aggregation produces `verdict="error"` per existing aggregation rule.

**DNS-rebinding caveat (documented, NOT mitigated this slice):** the helper resolves the hostname once for the check, then httpx resolves it again at fire-time. A hostile DNS server could return public on the first lookup and 169.254.169.254 on the second. Mitigations (resolve-then-pin-IP, custom `httpx.AsyncHTTPTransport`) are slice-3. Document the gap in code.

This is a guardrail, not a complete SSRF defense. Slice-3 can add a per-environment outbound allow-list.

### Twin override pattern — not breached

- Engine runs inside `BackgroundTasks` (dispatched at `routes.py:96`). It receives no inbound HTTP request and has no `Request` object. `_twin_overrides_var` (`shared/twin_middleware.py:24`) is a contextvar; FastAPI's BackgroundTask runs in the request's contextvar copy, so the engine *could* read it. **MUST NOT.** The engine does not (and will not) consult `get_twin_overrides()` — it imports from `api.shared.db`, `api.environments.substitution` (post-refactor), `api.shared.events`, `httpx`, and nothing else. If a future engineer wires twin-overrides into engine outbound calls, they are punching a hole through the staging/production strip (the contextvar IS populated in dev, but the engine being twin-aware would be a footgun: operator authors `requests.url=…`, expects deterministic outbound, twin redirects it elsewhere). **Documented in `api/runs/engine.py` module docstring: "engine does not consult X-Twin-* overrides; outbound URL is exactly what `requests.url` resolves to after `{{VAR}}` substitution."**

### Secrets at rest and in flight

- **At rest:** `environments.secrets` is Fernet-encrypted (`api/shared/secrets.py`'s `encrypt`/`decrypt`). DB stores ciphertext only. `_reveal_secret_map` decrypts in-process; never persisted post-decrypt.
- **In transit (engine → target):** `httpx.AsyncClient()` with `certifi` defaults. **HARD RULE: no `verify=False` anywhere.** Reviewer rejects any PR that adds it. No custom CA bundles this slice.
- **In memory:** decrypted secrets live in the `subs` dict for the duration of `execute_run`. Not logged, not pickled, not sent to Sentry (Sentry DSN config exists at `config.py:165` — confirm `before_send` strips request bodies, or set `send_default_pii=False`; slice-3 hardens further).
- **Header injection sanitizer:** after substitution, validate every header *value* matches `^[\x20-\x7e]*$` (printable ASCII, no CR/LF/NUL). On violation, drop the header from the outbound request AND record `substitution_missed`-style entry (or a new `header_rejected` field). This is belt-and-suspenders; httpx will likely reject too, but failing early with a clear evidence entry is better than `httpx.LocalProtocolError`.

### Mitigations summary (builder must implement)

1. **`CreateRunRequest.model_config = ConfigDict(extra="forbid")`** in `api/runs/schemas.py`. Reject `verdict`, `status`, `evidence`, `created_by_*`, `started_at`, `finished_at`, etc. at 422.
2. **`api/runs/_ssrf.py`**: `is_blocked_host(url, env) -> (bool, reason)` per spec above; call from `_execute_request` before `client.request(...)`. On block: synthesize a `transport_error` result, skip evaluations.
3. **Header-value sanitizer**: reject non-printable / CR / LF in post-substitution header values; drop the header, record in evidence.
4. **Secret-value redaction pass** on persisted `evidence.requests[*].url`, `response_headers` (values), `response_body_excerpt`: substring-replace any *secret* value (from `load_substitution_map`'s secrets-half) with `***`. Variables are NOT redacted. Implementation: pass the secret-value set into `_execute_request`, redact on the way into the result dict, before append.
5. **`captured` keys-only in persisted evidence**: `RunRequestResult.captured: list[str]` — enforced by schema type, not redaction. Engine keeps a local `captured_dict: dict[str, str]` for the in-process `subs` map; only `sorted(captured_dict.keys())` is written into the result. No code path writes captured *values* to JSONB.
6. **Engine MUST NOT log url/headers/body** at INFO. The `httpx` logger is forced to WARNING+ in production logging config.
7. **`verdict`/`status` post-INSERT writers**: only `api/runs/engine.py`. Code review rejects any other module that issues `UPDATE runs SET verdict=…` or `SET status=…`.
8. **Engine MUST NOT consult `get_twin_overrides()`**. Documented in engine module docstring; code review rejects the import.
9. **No `verify=False` on `httpx.AsyncClient`.** Ever.

### Open concerns (need user decision before build)

- **`evidence.requests[*].captured` keys-only — DECIDED (reconciled).** Schema type is `list[str]`; no further open question.
- **DNS rebinding** between SSRF check and fire is out of scope. Confirm acceptance — slice-3 fix is a pinned-IP `httpx.AsyncHTTPTransport`.
- **`response_body_excerpt` redaction may break diagnostics** (operator can no longer see the actual `Bearer XYZ` the target reflected). Trade is correct — leakage > debuggability — but flag for awareness.
- **No outbound rate limit** in slice-2. A harness key spamming `POST /v1/runs` against a collection of 50 requests can fire 50 outbound HTTP calls per spam. Acceptable for slice-2 (operators trust their own harnesses); slice-3 may add a per-harness-key QPS cap.
- **No bound on response body size before excerpt truncation** — `r.json()` / `r.text` materializes the full body in memory. A 1 GB response OOMs the worker. Recommend setting `httpx` `limits=httpx.Limits(...)` and a max-bytes read via streaming + cap, slice-2 or slice-3. Flag for builder to decide whether to in-scope.

### Security-focused test coverage (eval seeds)

The `platform/evals/seeds/pagehub_evals_runs_*.py` PR (SPEC.md:98-103) MUST include:

1. **PATCH 403 from operator JWT** → assert 403 on `PATCH /v1/runs/{id}`.
2. **PATCH 403 from harness key** → same, with `X-Harness-Key`.
3. **Harness-key cross-read** → key A creates run R; key B `GET /v1/runs/R` → 403.
4. **Operator JWT with wrong `app_slug`** → 403 at `dependencies.py:73-74`. (May already exist in slice-1 auth seed; if so, reference it; if not, add.)
5. **`CreateRunRequest` extra-field rejection** → POST with `{"verdict": "passed", …}` → 422.
6. **SSRF deny-list** (staging-only assertion): create a `requests` row with `url=http://169.254.169.254/latest/meta-data/`, POST a run, poll to terminal → `verdict="error"`, `evidence.requests[0].transport_error` contains `"blocked"`. Eval is skipped in `env=development` (where the deny-list is off by design).
7. **Header sanitizer** → environment variable containing `"abc\r\nX-Injected: yes"`, request with `Authorization: Bearer {{TOKEN}}`, run terminal evidence shows header was rejected/sanitized; no `X-Injected` reached the target (harness-side twin assertion per `CLAUDE.md` "Eval assertion" rule).
8. **Secret-value redaction** → environment secret `API_KEY=supersecret`, target deliberately echoes it in response body; `evidence.requests[0].response_body_excerpt` contains `***` not `supersecret`.

Items 1, 2, 3, 5 are non-negotiable. Items 6, 7, 8 enforce the slice-2 NEW security guarantees; if the builder ships without these seeds, the guarantees are claimed-not-checked.

AGREE: yes

SCORE: 3

## Reliability

### Failure modes

- **Worker death mid-run.** `BackgroundTasks` is per-uvicorn-worker, non-durable (`api/runs/routes.py:96`). A restart between the `pending → running` UPDATE (`engine.py:260-263`) and the terminal UPDATE (`engine.py:334-344`) leaves the row at `status='running'` forever. SPEC defers the reaper to slice-3 (Architect: `SPEC.md:499`). User-facing patch lives in the Designer's 5-minute detail-screen poll cap (`SPEC.md:244-250`) which then shows a manual Retry. Documented gap, not built this slice.
- **HTTP timeout per outbound request.** Hard 10s ceiling (`engine.py:171`). Sane; keep. Total run wall time is bounded ≈ 10s × N (no parallelism). To stop a single run from pinning a worker indefinitely, enforce a collection-size cap of **50 items** at `POST /v1/runs` time (see Reliability-critical items below) → ≈500s worst case per run.
- **Pool starvation.** Pool is 10 conns, `command_timeout=30` (`api/shared/db.py:17-23`). Current engine holds ONE connection across all HTTP fires (`engine.py:250-353`) — direct violation of Architect rule 4 (`SPEC.md:494-498`). Required reshape: (a) acquire-1 → write `started_at`, `status='running'` with `WHERE id=$1 AND status='pending'` guard, batch-fetch all `(requests, evaluations)` rows for the collection, RELEASE; (b) fire HTTP with NO DB conn held; (c) acquire-2 → single terminal UPDATE + `run.completed` event, RELEASE. Two short acquires per run, never N+1, never held across `await client.request(...)`.
- **Substitution miss.** `{{VAR}}` left literal when key absent (`engine.py:35-49`, policy at `SPEC.md:454`). A miss is **NEVER** a `transport_error` on its own — the request still fires with the literal token in url/headers/body. Misses propagate into eval observed-vs-expected through the (mis-substituted) outbound payload and the response it generates. Per-request `substitution_missed: list[str]` is populated by re-scanning the rendered payload with `re.findall(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}", ...)` (`SPEC.md:454`) and is what the UI consumes — no client-side regex.
- **Capture overrides env vars.** Engine policy: a value captured from response N via JSONPath (`requests.capture`, `SPEC.md:455-459`) is written into the run-local `subs` map and TAKES PRECEDENCE over the same key from `load_substitution_map(...)` when substituting requests N+1…M. Last-write-wins within the dict. Documented so operators don't see "wrong env value" and assume a bug.
- **httpx transient errors** (DNS NXDOMAIN, TCP RST, TLS handshake fail, ReadTimeout, ConnectError). One try, zero retries this slice. The except branch at `engine.py:183-184` captures `f"{type(e).__name__}: {e}"` into the per-request `transport_error` string. Slice-3 may add bounded retries; not now.
- **Database connectivity transient failure during terminal UPDATE.** If acquire-2 fails or the UPDATE itself raises, the run row sticks at `status='running'`. Same recovery gap as worker-death; covered by the slice-3 reaper. Reliability does NOT add a try/except retry around the terminal UPDATE this slice — surfacing failure to logs/Sentry is more honest than masking it.
- **Concurrent terminal UPDATEs** (double `BackgroundTasks` dispatch on the same `run_id`). The `WHERE status='running'` guard on the terminal UPDATE (`SPEC.md:498`) makes the second writer match 0 rows and silently no-op. First writer wins; verdict + evidence are written exactly once.

### Idempotency

- **`POST /v1/runs` is NOT idempotent this slice.** No client `Idempotency-Key` header is accepted or honored. A double-POST produces two distinct run rows, two engine dispatches, two verdicts. Operators that care about dedup can correlate on `harness_id` + `harness_claim` + a recent time window; the engine will not. Documented constraint; revisit in slice-3 only if harnesses retry POST under failure (no evidence yet that they do).
- **`execute_run(run_id)` IS idempotent under the status guards.** Calling it twice on the same row:
  - If the run is still `pending`: first call wins the `pending → running` guarded UPDATE (engine must add `AND status='pending'` per Architect rule 2, `SPEC.md:495`); second call updates 0 rows and `return`s silently.
  - If the run is already `running`: second call's `pending → running` UPDATE matches 0 rows; the engine must abort BEFORE firing any HTTP (currently it doesn't — `engine.py:260-263` is unguarded; fix it).
  - If the run is already terminal: terminal UPDATE's `WHERE status='running'` matches 0 rows; no clobber.
- **`harness_claim`, `verdict`, `status`, `evidence` are write-once / engine-only.** Enforced at the HTTP layer by blanket `PATCH /v1/runs/{id}` → 403 (`routes.py:145-154`) and at the engine layer by the guarded terminal UPDATE. Both layers tested.

### Retries & backoff

- **HTTP (outbound from engine):** 0 retries this slice. One attempt, 10s timeout, `transport_error` recorded on failure. The eval seed authoring on `platform` MUST NOT encode flake-prone assertions against unstable upstreams — the gate's verdict is only as stable as the deployed system it points at. Documented as a hard constraint on the seed.
- **Engine (execute_run itself):** 0 self-retries, 0 requeue. A crash mid-run leaves a stuck `running` row; recovery is the slice-3 reaper, not retry. This is intentional — silent retry of a partially-applied run that has already issued side-effecting POSTs against a deployed system would be worse than a stuck row.
- **DB (asyncpg):** 0 application-level retries. `command_timeout=30` (`api/shared/db.py:21`) bounds each statement. Pool itself reconnects opportunistically; we don't second-guess it.

### Observability

- **Events** (audit trail via `record_event`, `api/shared/events.py`):
  - `run.created` — emitted at `routes.py:84-95`. Actor = user or harness_key. Payload `{harness_id, collection_id}`. Keep.
  - `run.started` — emitted at `engine.py:264-272`. Actor = system. Payload `{}`. Keep.
  - `run.completed` — emitted at `engine.py:345-353`. Actor = system. **Extend payload** to `{verdict, status, request_count, duration_ms}`. `duration_ms` is `int((finished_at - started_at).total_seconds() * 1000)`; `request_count = len(evidence.requests)`. These two fields are the cheapest observability win this slice.
  - **Not emitted:** per-failing-evaluation events. High cardinality, low value vs the JSONB `evidence` blob which already carries the same info structurally.
- **Prometheus metrics** (project already has `/metrics` per `api/main.py:124-130`):
  - `pagehub_evals_runs_total{verdict}` — counter, labels `passed|failed|error`. Increment in engine's terminal write path AFTER the UPDATE succeeds.
  - `pagehub_evals_run_duration_seconds` — histogram, labelled by terminal verdict. Buckets: `0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600` (covers sub-second through the 500s implicit cap).
  - `pagehub_evals_run_request_count` — histogram, no labels. Buckets: `0, 1, 2, 5, 10, 20, 50`.
  - **Do NOT label by `harness_id`.** Cardinality blows up with every new harness key.
- **Logging.**
  - INFO: `run.started` and `run.completed` summary lines (`run_id`, `verdict`, `status`, `request_count`, `duration_ms`). NO request/response bodies, NO header values, NO env-var values (env may carry secrets per `load_substitution_map`).
  - WARN: any request with non-empty `substitution_missed` (one line per run, list the missed keys de-duped). SSRF deny-list hit (covered in Security, surfaces here as a WARN).
  - ERROR: terminal UPDATE failed (raises → caught at engine top-level wrapper); schema-apply failure at boot (already raises out of `lifespan` and aborts startup, `api/main.py:67-68`).
  - DEBUG (off by default): per-request `method`, `host` (URL hostname only — NOT the full post-substitution URL, which can carry secrets in querystrings), `status`, `latency_ms`. No path/query, no headers, no bodies.
- **Sentry** (`api/main.py:33-56`). Catch ONLY unexpected errors at the engine top-level — wrap the body of `execute_run` in try/except, capture-and-reraise via `sentry_sdk.capture_exception(...)` if available. Expected outcomes — per-request `transport_error`, eval mismatches, substitution misses, empty collection, `collection_id IS NULL` — are NOT Sentry-worthy and MUST NOT page on-call.

### Recovery / on-call

- **Stuck-running run.** No remediation endpoint this slice. On-call procedure: (a) confirm uvicorn was restarted by checking `/health` `git_sha` vs the row's `started_at`; (b) issue manual `UPDATE runs SET status='error', verdict='error', finished_at=now() WHERE id=$1 AND status='running'` against the platform Supabase DB. Slice-3 ships the reaper plus a `GET /v1/runs?status=running&older_than=5m` filter; until then operator triage is direct DB. Document this in the runbook.
- **Schema-apply failure at boot.** Existing lifespan behavior (`api/main.py:67-68`) raises out of `apply_schema()` and the app refuses to serve. `/health` then fails (process is down, not serving HTTP at all). Deploy gates on `/health` returning 200 — Vercel's deploy will not promote a broken build. No change needed this slice; just verify the `requests.capture` migration doesn't break the apply path.
- **Migration safety.** The one schema delta this slice is:
  ```sql
  ALTER TABLE requests ADD COLUMN IF NOT EXISTS capture JSONB NOT NULL DEFAULT '{}'::jsonb;
  ```
  Idempotent under the existing `apply_schema()` lifespan call. Safe to ship the migration BEFORE the engine code that reads it — existing rows backfill to `'{}'` and the column is a no-op for them. Rollout order: (1) migration deploys; (2) engine + routes deploy; (3) `RUNS_ENABLED=true` flips. Each step is independently rollback-safe.

### Health checks

- `/health` exists (`api/main.py:113-121`); reports `git_sha` and `env`. Do NOT add a runs-engine-specific health endpoint — there is no engine daemon in slice-2 to be healthy or unhealthy; `BackgroundTasks` is per-request.
- "Engine is alive" signal in slice-3 is "saw a `run.completed` event in the last N minutes" — derived from the existing events table or the Prom counter above. Alerting on absence belongs to platform/eyes, not this repo.

### Performance budgets

- Per outbound HTTP request: 10s hard timeout (`engine.py:171`).
- Per run wall time: ≤ 50 × 10s = **500s**, contingent on the 50-item collection cap below. Without the cap this is unbounded.
- `GET /v1/runs/{id}`: target p95 < 50ms (single row, single JSONB blob, no joins). The detail screen polls at 2s while running (`SPEC.md:244-250`), so each operator session generates ≤30 reads/min — trivial.
- 2s detail-poll × concurrent operators on the same run: at slice-2 scale (≤10 concurrent operators) this is ≤5 rps on the hot row, fine on the 10-conn pool.

### Reliability-critical items for the builder

Non-negotiable; reviewer will check each.

1. **Engine batches reads up front.** ONE connection acquire that writes `pending → running` (guarded) AND fetches `(collection_items, requests, evaluations)` for the whole run. Release before the first HTTP fire. No DB conn held across `await client.request(...)`.
2. **Two-acquire connection lifecycle.** Acquire-1 (pre-flight) + Acquire-2 (terminal UPDATE + final event). Nothing else. Replace current single-acquire pattern at `engine.py:250-353` end-to-end.
3. **Guarded `pending → running` UPDATE.** Add `AND status='pending'` to the UPDATE at `engine.py:260-263`. If 0 rows updated, `return` immediately — someone else already dispatched. No HTTP fires, no terminal write, no event.
4. **Guarded terminal UPDATE.** Add `AND status='running'` to the terminal UPDATE at `engine.py:334-344`. Already mandated by Architect; restated here so it's visible to reliability review.
5. **Collection-size cap.** `POST /v1/runs` returns **422** if `collection_id` points to a collection with > 50 items. Check by `SELECT count(*) FROM collection_items WHERE collection_id=$1` inside the same handler before the INSERT. Detail message: `"collection exceeds max items=50 (got N)"`. Bounds engine wall-time; tested below.
6. **10s timeout hard-coded.** No env-var override; no per-request override. Keep `engine.py:171` literal.
7. **No request-body / header / env-var values in logs.** Names of missed substitution keys are fine (they are config, not secrets). Captured values, response bodies, request bodies, header values — never logged.
8. **Engine top-level try/except.** `execute_run` wraps its full body so an unexpected exception (a) is sent to Sentry, (b) attempts a best-effort terminal UPDATE to `status='error', verdict='error', evidence={"requests": partial_results, "engine_error": str(exc)}`. If the best-effort UPDATE itself fails, log ERROR and let the slice-3 reaper handle it.
9. **Drop the JSON deep-copy at `engine.py:227`.** Wasteful per Architect (`SPEC.md:382`); httpx returns fresh objects.

### Test coverage

- **Unit tests** (`api/tests/test_runs_engine.py` — new):
  - `test_substitute_leaves_missing_keys_literal` — `_substitute("{{X}}", {})` returns `"{{X}}"`. Proves miss-policy.
  - `test_substitute_capture_overrides_env_var` — given subs seeded `{"K":"env"}` then updated `{"K":"captured"}`, the next substitution returns `"captured"`. Proves capture-precedence invariant.
  - `test_resolve_path_missing_returns_sentinel` — `$.a.b` on `{"a":{}}` returns `_MISSING`. Proves JSONPath-lite miss semantics.
  - `test_verdict_aggregation_transport_error_dominates` — given two requests, one with `transport_error`, one with all-passed evals → run verdict = `"error"`. Proves Architect's aggregation rule (`SPEC.md:474`).
  - `test_verdict_aggregation_no_evaluations_is_error` — collection with one request, zero evaluations attached → verdict = `"error"`. Proves "nothing was actually checked" branch.
  - `test_verdict_aggregation_one_failing_eval_is_failed` — one request, one passing eval, one failing eval → verdict = `"failed"` (not `error`). Proves `failed` vs `error` distinction.
  - `test_terminal_update_no_op_when_already_terminal` — invoke engine path against a row pre-set to `status='passed'`; assert UPDATE matches 0 rows and no second event emitted. Proves idempotency guard.
  - `test_collection_size_cap_enforced_at_post` — `POST /v1/runs` with a collection of 51 items → 422 with detail `"collection exceeds max items=50 (got 51)"`. Proves the wall-time bound.
- **Eval coverage** (`~/github/pagehub-io/platform/evals/seeds/pagehub_evals_runs.py` — new; copy the structure of the sibling `pagehub_evals_evaluations.py`):
  - Spec markdown: `~/github/pagehub-io/platform/evals/specs/pagehub-evals/runs.md` — narrative for the reliability scenarios below + the SPEC's slice-2 success-criteria scenarios (PATCH-403, capture+substitute, failing-eval verdict).
  - Seed request prefixes + captures + key assertions:
    - `runs_create_basic` → `POST /v1/runs` with seeded collection+env, capture `RUN_ID = $.id`. Evals: status 202, `$.status == "pending"`, `$.verdict == null`.
    - `runs_poll_to_terminal` → `GET /v1/runs/{{RUN_ID}}` loop (seed harness handles the polling per `_shared.py` pattern). Evals: terminal `$.status in {passed, failed, error}`, `$.evidence.requests` is a list.
    - `runs_harness_claim_immutable` → re-GET after terminal, assert `$.harness_claim` byte-equal to the original POST body.
    - `runs_patch_blocked_operator` → `PATCH /v1/runs/{{RUN_ID}}` as operator JWT, expect 403.
    - `runs_patch_blocked_harness` → same as above, harness key, expect 403.
    - `runs_oversize_collection_rejected` → seed a 51-item collection, POST against it, expect **422** + body contains `"max items=50"`. (Reliability-specific.)
    - `runs_transport_error_is_error_verdict` → seed a collection whose request points at an unroutable host (e.g. `http://127.0.0.1:1` or a known-dead twin URL); poll to terminal, expect `$.verdict == "error"`, `$.evidence.requests[0].transport_error` non-null, `$.evidence.requests[0].response_status == 0`. (Reliability-specific.)
    - `runs_substitution_miss_propagates` → seed a request whose body references `{{NOT_IN_ENV}}`; poll to terminal, expect `$.evidence.requests[0].substitution_missed` contains `"NOT_IN_ENV"` AND that this did NOT manifest as `transport_error`. (Reliability-specific.)
    - `runs_slow_target_times_out_at_10s` → twin endpoint that sleeps 15s; poll to terminal, expect `$.evidence.requests[0].transport_error` references a timeout class, `$.verdict == "error"`, and total `(finished_at - started_at) < 12s`.
- **Concurrency / fault injection** (`api/tests/test_runs_engine_concurrency.py` — new, runs against the `db` Compose service, not a mock):
  - `test_double_dispatch_writes_terminal_once` — INSERT a run row directly, fire `execute_run(run_id)` twice via `asyncio.gather` against a real Postgres, assert exactly one `run.completed` event and one terminal row. Proves the `WHERE status='running'` guard.
  - `test_schema_apply_is_idempotent` — call `apply_schema()` twice in succession against the same DB; second call must not raise. Proves migration safety for the `requests.capture` `ADD COLUMN IF NOT EXISTS` line.
  - `test_pool_not_held_during_http_fire` — patch `_execute_request` to await a long sleep; assert during that sleep the pool's `get_size() - get_idle_size()` is 0 (engine holds no conn while HTTP is in flight). Proves the two-acquire rule structurally.
- **Open coverage gaps** (explicit, not silently dropped):
  - Worker-death mid-run is NOT covered by a test this slice. The path that produces a stuck row exists; the reaper that recovers from it does not. Slice-3 ships both the reaper and a test that asserts a row > 5min old in `running` gets swept to `error`.
  - Pool-starvation under load (e.g., 11 concurrent runs against the 10-conn pool) is not load-tested this slice. The two-acquire lifecycle defends against it structurally; a dedicated load test waits until we have real concurrent harnesses.
  - Sentry plumbing (engine top-level wrapper actually sends to Sentry) is not asserted in CI — Sentry isn't wired in `env=test`. Manual verification on staging only.

AGREE: yes

SCORE: 3

## Data

### Shapes

#### Schema changes (idempotent SQL, applied via lifespan)

One DDL line added to `api/shared/schema.sql`, placed immediately after the existing `body` column in the `requests` block (`schema.sql:32`):

```sql
ALTER TABLE requests ADD COLUMN IF NOT EXISTS capture JSONB NOT NULL DEFAULT '{}'::jsonb;
```

- Placement: directly below the `CREATE TABLE requests (...)` block, between `schema.sql:35` and the existing `CREATE INDEX IF NOT EXISTS requests_owner_idx` at `schema.sql:36`. Keep the `CREATE TABLE` block as the historical baseline; the `ALTER ... ADD COLUMN IF NOT EXISTS` line lives below so the column materialises on first boot AND on every subsequent boot against pre-existing rows. Idempotent.
- **No migrations folder.** `api/shared/schema.py:14-24` applies `schema.sql` end-to-end on every lifespan boot under advisory lock `0xE5_4A_15_00` inside a single transaction. The builder MUST express schema evolution as additional `ALTER ... IF NOT EXISTS` / `CREATE ... IF NOT EXISTS` statements appended to `schema.sql` — never as a `migrations/` directory, Alembic, or a separate runner. This is the contract.
- `runs` (`schema.sql:97-111`): **no DDL changes.** All required columns exist — `harness_claim TEXT`, `verdict TEXT`, `status TEXT NOT NULL DEFAULT 'pending'`, `evidence JSONB NOT NULL DEFAULT '{}'::jsonb` (`schema.sql:107`), `created_by_kind`, `created_by_id`, `started_at`, `finished_at`. Evidence column type stays `JSONB NOT NULL DEFAULT '{}'::jsonb` — the engine writes `{"requests": [...]}` into it via `json.dumps(...)` cast `::jsonb`.
- `evaluations`, `collection_items`, `collections`, `environments`, `harness_keys`, `events`: unchanged.

#### Pydantic shape additions

`api/runs/schemas.py` — add three typed-evidence models and tighten `CreateRunRequest`:

```python
class RunEvaluationResult(BaseModel):
    id: UUID
    name: str
    kind: str
    passed: bool
    detail: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None

class RunRequestResult(BaseModel):
    request_id: UUID
    request_name: str
    method: str
    url: str
    response_status: int
    response_headers: dict[str, str] = Field(default_factory=dict)
    response_body_excerpt: Any = None
    latency_ms: int
    transport_error: str | None = None
    substitution_missed: list[str] = Field(default_factory=list)
    # KEYS ONLY. Captured *values* often carry live session tokens; the schema
    # enforces "no values persisted" by TYPE (list, not dict). Engine builds a
    # local `captured_dict: dict[str, str]` for the in-process subs map, then
    # writes `sorted(captured_dict.keys())` here. See Security keys-only rule.
    captured: list[str] = Field(default_factory=list)
    evaluations: list[RunEvaluationResult] = Field(default_factory=list)
    passed: bool

class RunEvidence(BaseModel):
    requests: list[RunRequestResult] = Field(default_factory=list)
```

- `RunResponse.evidence` (`api/runs/schemas.py:46`) changes from `dict[str, Any]` to `RunEvidence`.
- `CreateRunRequest` (`api/runs/schemas.py:29`) gains `model_config = ConfigDict(extra="forbid")` — a client POST attempting to set `verdict` / `status` / `evidence` returns 422 at the HTTP boundary, not silent-drop. Schema-level enforcement of write-once that mirrors the route-level PATCH-403.
- `_row_to_response` in `api/runs/routes.py:36-54` round-trips evidence through `RunEvidence.model_validate(...)` before constructing `RunResponse`. **Ordering is load-bearing: decode-to-dict-first, then fallback, then schema-validate.** (1) Read `evidence = row["evidence"]`; if asyncpg returned a JSON string (driver-version-dependent), `json.loads(evidence)` first. (2) Fallback: `evidence = evidence or {"requests": []}` so the INSERT-default `'{}'::jsonb` validates against the new typed shape rather than tripping `requests` as required. (3) `RunEvidence.model_validate(evidence)`. **No try/except** around `model_validate` — an engine-written row that fails validation is a real bug and must surface as 500, not silently empty the evidence.
- Cross-section note (Security): the Security plan's keys-only rule (`PLAN.md:43,88`) is now enforced at the SCHEMA layer — `RunRequestResult.captured: list[str]`. No redaction pass needed over `captured`; the type makes "values persisted" structurally impossible. Engine builds a local `captured_dict: dict[str, str]` for the in-process subs map and writes only `sorted(captured_dict.keys())` into the persisted result.

`api/requests/schemas.py` — add `capture` to all three shapes:

```python
class CreateRequestRequest(BaseModel):
    # ... existing fields ...
    capture: dict[str, str] = Field(default_factory=dict)

    @field_validator("capture")
    @classmethod
    def _validate_capture(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > 32:
            raise ValueError("at most 32 captures per request")
        for k in v:
            if len(k) > 64:
                raise ValueError(f"capture key too long: {k!r}")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
                raise ValueError(f"capture key must match ^[A-Za-z_][A-Za-z0-9_]*$: {k!r}")
        return v

class UpdateRequestRequest(BaseModel):
    # ... existing optional fields ...
    capture: dict[str, str] | None = None

class RequestResponse(BaseModel):
    # ... existing fields ...
    capture: dict[str, str] = Field(default_factory=dict)
```

- Key shape: `^[A-Za-z_][A-Za-z0-9_]*$`, max 64 chars, max 32 captures per request. Values are JSONPath-lite expressions (`$.id`, `$.a.b[0]`) — validated only at engine eval time, not at write time, because the engine's `_resolve_path` (`engine.py:60-98`) is the authoritative parser. A malformed path returns `_MISSING` at run time, surfaces as an empty `captured` map. No write-time path validation.
- Seed-compatibility check: the slice-1 seed at `~/github/pagehub-io/platform/evals/seeds/pagehub_evals_evaluations.py:63` POSTs `capture={"PARENT_REQ_ID": "$.id"}` — passes every rule above. No regression.

#### Engine JSONB write contract (`runs.evidence`)

Engine builds typed `RunRequestResult`s, wraps as `RunEvidence`, dumps to JSON via `model_dump(mode="json")`, casts `::jsonb` in the terminal UPDATE. Worked example for a 2-request run with one capture chained across them:

```json
{
  "requests": [
    {
      "request_id": "11111111-1111-1111-1111-111111111111",
      "request_name": "create-thing",
      "method": "POST",
      "url": "https://api.example/things",
      "response_status": 201,
      "response_headers": {"content-type": "application/json"},
      "response_body_excerpt": {"id": "abc"},
      "latency_ms": 123,
      "transport_error": null,
      "substitution_missed": [],
      "captured": ["THING_ID"],
      "evaluations": [
        {
          "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
          "name": "created",
          "kind": "status_eq",
          "passed": true,
          "detail": {"observed": 201, "expected": 201},
          "error": null
        }
      ],
      "passed": true
    },
    {
      "request_id": "22222222-2222-2222-2222-222222222222",
      "request_name": "fetch-thing",
      "method": "GET",
      "url": "https://api.example/things/abc",
      "response_status": 200,
      "response_headers": {"content-type": "application/json"},
      "response_body_excerpt": {"id": "abc", "status": "ok"},
      "latency_ms": 87,
      "transport_error": null,
      "substitution_missed": [],
      "captured": [],
      "evaluations": [
        {
          "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
          "name": "status",
          "kind": "status_eq",
          "passed": true,
          "detail": {"observed": 200, "expected": 200},
          "error": null
        }
      ],
      "passed": true
    }
  ]
}
```

Shape-drift vs. current WIP `engine.py:231-244`: today's engine writes a nested `response: {status_code, headers, body_excerpt}` sub-object. The typed contract flattens to `response_status` / `response_headers` / `response_body_excerpt` and adds `transport_error` (replacing the loose `error` key), `substitution_missed`, `captured`, `passed`. Engine refactor adopts the new shape; old rows are not migrated — the feature has been 503-flagged so no production rows exist.

#### Redaction-pass ordering (mandatory, in this order)

Validation runs AFTER redaction, not before. Builder MUST follow these four steps for each per-request result:

(a) **Engine builds the raw result as a plain Python dict.** Post-substitution `url` (string), raw `response_headers: dict[str, str]` straight from `httpx`, raw `response_body_excerpt` (str | dict | list, already truncated to 1000 chars). No Pydantic yet.

(b) **`_redact_secrets(secret_values_set, result_dict)` mutates in place.** The function walks `result_dict["url"]`, every value in `result_dict["response_headers"]`, and `result_dict["response_body_excerpt"]` (recursively if dict/list, substring-replace if str). For each occurrence of any value in `secret_values_set` (the *secrets* half of `load_substitution_map`, never variables), replace with `***`. Mutation, not copy — the dict passed in is the dict serialised at the end.

(c) **Engine builds `captured: list[str]` from local `captured_dict.keys()`.** `captured_dict` lives only in the engine's run-local scope and feeds the next request's `subs` map; only the sorted keys are written into `result_dict["captured"]`. No redaction pass over `captured` — the schema's `list[str]` type makes value-leakage structurally impossible (see schema note above).

(d) **`RunRequestResult.model_validate(result_dict)` then `RunEvidence.model_dump(mode="json")` for the `::jsonb` bind.** Validation is the LAST step. If redaction was skipped, validation will still pass (the schema doesn't know what a secret looks like); the contract is "redaction precedes validation precedes persist." Builder note: do not validate the result first and then try to redact a `RunRequestResult` instance — Pydantic-v2 models are not freely mutable and the ordering invites silent bugs.

### Migrations

| Step | Forward | Rollback | Online-safe? |
|---|---|---|---|
| 1 | Append `ALTER TABLE requests ADD COLUMN IF NOT EXISTS capture JSONB NOT NULL DEFAULT '{}'::jsonb;` to `api/shared/schema.sql` | `ALTER TABLE requests DROP COLUMN IF EXISTS capture;` (manual, not committed) | **Yes.** `ADD COLUMN ... DEFAULT '{}'::jsonb` with a constant default is metadata-only in Postgres 11+. No table rewrite, no row scan. Applied inside the lifespan transaction under advisory lock `0xE5_4A_15_00` so concurrent worker boots serialise. |
| 2 | (Pydantic-only) `_row_to_response` in `api/runs/routes.py` round-trips evidence through `RunEvidence.model_validate(...)` | revert code | Yes — no DB change. |
| 3 | (Pydantic-only) `CreateRunRequest` gains `extra="forbid"` | revert code | Yes — no DB change. |
| 4 | (Optional) `CREATE INDEX IF NOT EXISTS runs_created_at_idx ON runs (created_at DESC);` appended to `schema.sql` | `DROP INDEX IF EXISTS runs_created_at_idx;` | Yes for slice-2 size. Index creation runs inside the schema transaction (NOT `CONCURRENTLY`) — briefly takes `ACCESS EXCLUSIVE` on `runs`. Fine while `runs` is small; if the table grows past ~100k rows before slice-3 lands, split into a separate non-transactional path. |

**No backfill needed.** `requests.capture` is `NOT NULL DEFAULT '{}'::jsonb` so existing rows pick up the default atomically at column-add. No data migration step.

### Queries

In execution order for one run lifecycle. Placeholders are asyncpg-style `$1, $2, ...`.

1. **`POST /v1/runs`** — `api/runs/routes.py:64-83`, **unchanged**:
   ```sql
   INSERT INTO runs (
       created_by_kind, created_by_id,
       collection_id, environment_id,
       harness_id, harness_claim,
       status
   ) VALUES ($1, $2, $3, $4, $5, $6, 'pending')
   RETURNING id, created_by_kind, created_by_id, collection_id, environment_id,
             harness_id, harness_claim, status, verdict, evidence,
             started_at, finished_at, created_at;
   ```
   Bindings: `auth.actor_kind, auth.actor_id, body.collection_id, body.environment_id, body.harness_id, body.harness_claim`.

2. **Engine — mark running** (`engine.py:260-263`, **refactor** — add guard + RETURNING):
   ```sql
   UPDATE runs
   SET status = 'running', started_at = now()
   WHERE id = $1 AND status = 'pending'
   RETURNING id;
   ```
   If 0 rows returned → engine aborts silently (re-fire defense, Architect Concurrency rule 2, `SPEC.md:495-496`). Connection released immediately after this UPDATE + event write; HTTP loop runs with no DB connection held.

3. **Engine — load environment substitution map** — one call to `api.environments.substitution.load_substitution_map(conn, environment_id)` (new non-FastAPI module per `SPEC.md:390`). Existing SQL shape unchanged.

4. **Engine — batched read of collection items + requests + evaluations.** Replaces the N+1 per-request fetches at `engine.py:282-311`. **Call: two queries**, not a single `json_agg` LEFT JOIN — simpler asyncpg row handling, equivalent perf at the slice's scale (handful of items, single-digit evals per request).

   Query 4a — items + requests (single JOIN, ordered):
   ```sql
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
   ORDER BY ci.position ASC;
   ```

   Query 4b — all evaluations for those requests, fetched in one round-trip:
   ```sql
   SELECT id, request_id, name, kind, config
   FROM evaluations
   WHERE request_id = ANY($1::uuid[])
   ORDER BY request_id, created_at ASC;
   ```
   Bindings for 4b: a `list[UUID]` of `request_id`s pulled from 4a's rows. Engine groups by `request_id` in Python before entering the HTTP loop. Connection released BEFORE the first `httpx` fire (Concurrency rule 4, `SPEC.md:497`).

   **Why two queries over `json_agg`:** asyncpg returns `json_agg` as a JSON string requiring an extra `json.loads` per row, and the slice's scale doesn't justify the complexity. Two prepared statements, one round-trip each, total 2 round-trips per run regardless of item count. N+1 eliminated.

5. **Engine — terminal UPDATE** (`engine.py:334-344`, **refactor**: add `WHERE status='running'` guard, swap to typed evidence dict):
   ```sql
   UPDATE runs
   SET status = $1, verdict = $2, evidence = $3::jsonb, finished_at = now()
   WHERE id = $4 AND status = 'running';
   ```
   Bindings: `status, verdict, json.dumps(evidence.model_dump(mode="json")), run_id`. The `WHERE status='running'` guard makes the terminal write a no-op against a row that's already terminal — protects against double-dispatch of `execute_run` on the same `run_id` (`SPEC.md:498`). If the UPDATE reports 0 rows affected, engine SKIPS the `run.completed` event so the audit log doesn't claim completion the row didn't accept.

6. **`GET /v1/runs`** — `api/runs/routes.py:105-115`, **unchanged**. Ordered `created_at DESC`, `LIMIT 500`. 1 query per response, ≤500 rows. Each row's `evidence` JSONB is parsed once in `_row_to_response`.

7. **`GET /v1/runs/{id}`** — `api/runs/routes.py:124-133`, **unchanged**. Single PK lookup. Hot path under Designer's 2s auto-poll (`SPEC.md:244-250`).

8. **`PATCH /v1/runs/{id}`** — `api/runs/routes.py:145-154`. No DB query at all; raises 403 immediately after `_gate()`. Already correct.

#### Request CRUD query updates (capture column)

- `INSERT` (`api/requests/routes.py:50-62`): add `capture` to column list + a `$7::jsonb` bind; pass `json.dumps(body.capture)`.
- `UPDATE` (when `UpdateRequestRequest` lands — currently not present in `api/requests/routes.py`): include `capture` in the partial-update set when `body.capture is not None`.
- `SELECT` (list + detail): add `capture` to the returned columns; `_row_to_response` parses JSONB → dict (existing `headers`/`body` parsing pattern at `routes.py:24-32` is the template).

#### Hot paths & latency targets

| Path | Query | Target | Notes |
|---|---|---|---|
| `GET /v1/runs/{id}` (auto-poll, 2s cadence) | PK lookup | <20ms | Single row, JSONB parse client-side. No new index needed (PK). |
| `GET /v1/runs` | seq scan + sort | <200ms at 10k rows | `LIMIT 500` caps work. `runs_created_at_idx` (below) removes the sort step. |
| Engine batched read (4a + 4b) | indexed JOIN + ANY-array | <50ms per run start | Both queries use existing indexes (`collection_items_collection_idx`, `evaluations_request_idx`). |
| Engine terminal UPDATE | guarded PK update | <10ms | One row, one JSONB write. |

### Indexes

Already present (`schema.sql:112-114`):

- `runs_actor_idx` on `(created_by_kind, created_by_id)` — services the harness-self-read clamp at `routes.py:137-138`.
- `runs_collection_idx` on `(collection_id)` — slice-3 filter; unused by slice-2 queries.
- `runs_status_idx` on `(status)` — services the future stale-run reaper's `WHERE status='running'` scan.

**New (recommended, cheap, future-proofs slice-3 pagination):**

```sql
CREATE INDEX IF NOT EXISTS runs_created_at_idx ON runs (created_at DESC);
```

- Rationale: `GET /v1/runs` does `ORDER BY created_at DESC LIMIT 500`. Below ~10k rows seq-scan + sort is fine; above that the sort dominates. Index is cheap to maintain on insert and removes the sort step from the list query — pure upside. Designer's slice-3 filter/pagination will lean on it directly.
- Caveat noted in Migrations step 4: applied inside the lifespan transaction (not `CONCURRENTLY`), so it briefly takes `ACCESS EXCLUSIVE` on `runs`. Fine while `runs` is small; revisit if the table grows past ~100k rows before slice-3.

No other new indexes needed:

- `requests.capture` is read by request_id (PK) inside query 4a — no separate index required.
- `evaluations.request_id` already indexed at `schema.sql:48`.

### Retention & PII

**No retention this slice.** Documenting explicitly:

- `runs` rows are **never pruned.** Verdict history is the audit substrate the JTBD relies on — pruning erases the very claims the operator went looking for. Slice-3 or later may add a retention policy (e.g., 90-day rollup for `passed` runs, indefinite for `failed`/`error`), but no policy ships this slice.
- `evidence` JSONB can grow up to ~50KB per run (response body excerpts capped at 1000 chars × N requests). At expected slice-2 scale (hundreds of runs/day across all harnesses) this fits comfortably in Postgres without partitioning.
- `harness_claim` (max 10 000 chars, `api/runs/schemas.py:33`) may contain agent-paste text. Not PII by source (agent output), but agents MAY include personal email addresses in claims. No special handling this slice — claims stored plaintext in `runs.harness_claim`.
- `environments.secrets` is already Fernet-encrypted at rest (`schema.sql:16`); decryption is in-memory only via `load_substitution_map`. **Secrets MUST NOT leak into `evidence`** — the Security plan (`PLAN.md:41,87`) requires a value-set redaction pass on `url`, `response_headers`, `response_body_excerpt` before persist. Data layer trusts Security to perform that pass before serialising `RunRequestResult` to JSON.
- `events` rows are append-only and never deleted (`schema.sql:116-127`). Same "no retention" posture.

### Integrity

#### Write-once invariants (engine + route are the sole writers)

| Field | Writer | Mechanism |
|---|---|---|
| `runs.harness_claim` | `POST /v1/runs` INSERT only (`routes.py:67-83`) | No code path UPDATEs it. PATCH → 403 (`routes.py:145-154`). Eval-seed asserts byte-equality across creation and every subsequent GET. |
| `runs.verdict` | Engine terminal UPDATE only (`engine.py:334-344`) | Guard `WHERE id=$1 AND status='running'` blocks double-write. INSERT defaults `verdict=NULL`. |
| `runs.status` | INSERT writes `'pending'`; engine writes `'running'` (guarded `WHERE status='pending'`) then terminal value (guarded `WHERE status='running'`) | No PATCH path. Three guarded transitions exhaust the state machine. |
| `runs.evidence` | Engine terminal UPDATE only | Validated client-side on read through `RunEvidence.model_validate`. Engine constructs from typed `RunRequestResult`s and serialises via `model_dump(mode="json")`. |
| `runs.created_by_kind`, `runs.created_by_id` | INSERT only, from `AuthContext` | No UPDATE path. PATCH → 403. |
| `runs.started_at`, `runs.finished_at` | Engine only (mark-running and terminal UPDATE respectively) | Never written together; never overwritten once set. |
| `requests.capture` | `POST` + (future) `PATCH /v1/requests` (operator-only, `require_user`) | Pydantic `dict[str, str]` shape enforced at write; key regex + 64-char + 32-entry caps. |
| Collection size at run dispatch | `POST /v1/runs` handler (route-level) | **No DB constraint.** Enforced in the route via `SELECT count(*) FROM collection_items WHERE collection_id=$1` before INSERT; > 50 → 422. The cap exists to bound engine wall-time (Reliability item 5) and is intentionally not a check constraint on `collection_items` — operators may author large collections for non-run uses; the cap only gates dispatch. |

#### Referential consistency

- `evaluations.request_id → requests(id) ON DELETE CASCADE` (`schema.sql:41`). Deleting a request wipes its evals.
- `collection_items.request_id → requests(id) ON DELETE CASCADE` (`schema.sql:65`). Engine's batched read 4a silently skips deleted requests because they no longer satisfy the JOIN — same effective behaviour as today's `if req_row is None: continue` at `engine.py:301`.
- `runs.collection_id` / `runs.environment_id → ON DELETE SET NULL` (`schema.sql:101-102`). A run whose collection or environment was deleted post-creation continues to live; `evidence` already records the substituted URL so the historical run is interpretable without the parent rows.

#### Transaction boundaries

- **POST /v1/runs**: INSERT + `record_event` execute on the request-scoped connection from `get_db`. Both succeed or both fail when the request unwinds.
- **Engine mark-running**: single UPDATE + single event INSERT, sequential on one short-lived connection. Both target idempotent writes.
- **Engine terminal**: single UPDATE + single event INSERT, sequential on one short-lived connection. The terminal UPDATE is guarded (`WHERE status='running'`); if it reports 0 rows affected, the engine MUST skip the event write so the audit log doesn't claim completion the row didn't accept.
- **Schema apply**: wrapped in one transaction under advisory lock `0xE5_4A_15_00` (`api/shared/schema.py:21-23`). Concurrent uvicorn workers serialise; partial-schema states impossible.

#### Race-free invariants

- **Re-fire of `execute_run` on the same `run_id`**: protected by two guards — mark-running `WHERE status='pending'` (rule 2) AND terminal `WHERE status='running'` (rule 5). Second dispatch will either fail to mark-running (status already moved) → abort silently, or succeed at mark-running iff the first dispatch already finished and the row is somehow back to `pending` (impossible — engine never writes `pending`). Safe.
- **Engine writing while a slice-3 stale-run reaper writes**: out of scope this slice. When the reaper lands, it MUST use `WHERE status='running' AND started_at < now() - interval '5 minutes'`, racing identically with the terminal UPDATE; whichever wins, the row goes terminal exactly once. Document the contract now so slice-3 doesn't re-derive it.

#### Defensive read-side validation

- `_row_to_response` (`api/runs/routes.py:36-54`): **decode-to-dict-first, then fallback, then schema-validate** — (1) `evidence = row["evidence"]`; if asyncpg returned a string, `json.loads` it; (2) `evidence = evidence or {"requests": []}` (covers the `'{}'::jsonb` INSERT default which would otherwise fail `RunEvidence` validation); (3) `RunEvidence.model_validate(evidence)`. **No try/except** around `model_validate` — an engine-written row that fails validation is a real engineering bug and must surface as 500, not silently empty the evidence.
- Engine reads `requests.capture` as raw JSONB; if the value is None / malformed (legacy NULL despite `NOT NULL DEFAULT`, or some future migration breakage), treat as empty dict (defensive). Pydantic enforces `dict[str, str]` on the response side.

### Backfill / migration

- **None.** `requests.capture` is `NOT NULL DEFAULT '{}'::jsonb`. Existing rows materialise the default at `ALTER TABLE` time. Idempotent re-runs of `schema.sql` no-op. No data movement.

### Data-layer items for builder (ordered)

1. Append the one DDL line to `api/shared/schema.sql` directly below the `CREATE TABLE requests (...)` block (between `schema.sql:35` and `schema.sql:36`) — exactly: `ALTER TABLE requests ADD COLUMN IF NOT EXISTS capture JSONB NOT NULL DEFAULT '{}'::jsonb;`
2. Append `CREATE INDEX IF NOT EXISTS runs_created_at_idx ON runs (created_at DESC);` after `schema.sql:114`.
3. Update `api/requests/schemas.py`: add `capture` to `CreateRequestRequest` (with `field_validator`), `UpdateRequestRequest` (optional), and `RequestResponse`. Import `re` and `field_validator` from `pydantic`.
4. Update `api/requests/routes.py`: INSERT/UPDATE/SELECT include `capture`; `_row_to_response` parses JSONB → dict; passes through to `RequestResponse`.
5. Update `api/runs/schemas.py`: add `RunEvaluationResult`, `RunRequestResult`, `RunEvidence`. Swap `RunResponse.evidence` type from `dict[str, Any]` to `RunEvidence`. Add `model_config = ConfigDict(extra="forbid")` to `CreateRunRequest`. Import `ConfigDict` from `pydantic`.
6. Update `api/runs/routes.py:36-54`: `_row_to_response` round-trips through `RunEvidence.model_validate(evidence or {"requests": []})`. No try/except — engine bugs surface as 500.
7. Refactor `api/runs/engine.py`:
   - Swap `from api.environments.routes import load_substitution_map` for `from api.environments.substitution import load_substitution_map` (Architect's Components, `SPEC.md:390`).
   - Replace held-connection pattern (`engine.py:250-353`) with three short-lived acquires: mark-running (with `WHERE status='pending'` guard + `RETURNING id`), batched read (queries 4a + 4b above), terminal UPDATE (`WHERE status='running'` guard).
   - Build per-request results as typed `RunRequestResult` (not raw dicts); aggregate into `RunEvidence`; persist via `json.dumps(evidence.model_dump(mode="json"))` for the `$3::jsonb` bind.
   - Track `substitution_missed: list[str]` per request using the authoritative regex `re.findall(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}", json.dumps(post_sub_payload))` (`SPEC.md:454`).
   - Apply captures post-`_execute_request`: maintain an engine-local `captured_dict: dict[str, str]` for the current request. For each `(name, path)` in `request_row["capture"]`, run `_resolve_path(response_body, path)`; if not `_MISSING`, set `subs[name] = str(value)` AND `captured_dict[name] = str(value)`. When building `RunRequestResult`, write `captured=sorted(captured_dict.keys())` — keys only, schema-enforced (`list[str]`). The real values stay in the engine-local `subs` map for downstream requests; they are never serialised.
   - Drop the `json.loads(json.dumps(...))` deep-copy at `engine.py:227` — `r.json()` returns fresh objects already.
   - Verdict aggregation: compute per-request `passed` first (`transport_error is None` AND evals non-empty AND all evals passed), then run-level per `SPEC.md:471-483`.
8. Confirm `api/environments/substitution.py` (the new non-FastAPI module per `SPEC.md:390`) is the import target — the data layer depends on this refactor landing so the engine doesn't transitively import FastAPI.

### Test coverage (data-focused, eval-seed required cases)

1. **`requests.capture` roundtrip.** POST a request with `capture={"PARENT_REQ_ID": "$.id", "AUTH": "$.token"}`. GET it back. Assert `response.capture == {"PARENT_REQ_ID": "$.id", "AUTH": "$.token"}` byte-equal (dict comparison). PUT/PATCH with `capture={"X": "$.x"}` replaces wholesale. Slice-1 seed at `~/github/pagehub-io/platform/evals/seeds/pagehub_evals_evaluations.py:63` already exercises this shape — must not regress.
2. **Capture key validation.** POST with `capture={"1bad": "$.x"}` → 422 (leading digit). POST with `capture={"x"*65: "$.x"}` → 422 (key too long). POST with 33 keys → 422 (too many).
3. **`RunEvidence` parses cleanly post-terminal.** Create a run that exercises 2 requests with capture/substitution; poll until terminal; `GET /v1/runs/{id}` returns an `evidence` payload that Pydantic-validates against `RunEvidence` without falling back to defaults — `requests` non-empty, every `RunRequestResult` has `passed: bool`, `response_status: int`, `substitution_missed: list`, `captured: list[str]` (keys-only by schema). Additionally assert no captured *value* leaked into the persisted blob: the response body of the run-detail GET MUST NOT contain any of the secret values the engine substituted (substring scan).
4. **Empty-collection edge case (`SPEC.md:73-79`).** POST a run with `collection_id=null`. Poll to terminal. Assert `status='error'`, `verdict='error'`, `evidence == {"requests": []}`, `harness_claim` preserved byte-equal to POST body, `started_at` AND `finished_at` both non-null.
5. **`runs.verdict` domain.** Across the full seed run, assert `verdict in {None, "passed", "failed", "error"}` at every observed state. `None` only while `status in {"pending", "running"}`.
6. **Write-once: `harness_claim` immutability.** POST with a 500-char claim including newlines and unicode. GET immediately (status `pending`). GET after terminal. Assert byte-equality across both reads and the original POST body.
7. **PATCH-403 from both auth kinds (`SPEC.md:50, 124`).** Mint a harness key, POST a run as that key, PATCH with empty body, PATCH with `{"verdict": "passed"}` — every PATCH returns 403 regardless of body content. Repeat as operator JWT.
8. **`CreateRunRequest.extra="forbid"`.** POST with `{"verdict": "passed", "harness_claim": "I did it"}` → 422 (extra-field rejected at schema layer, not 202-with-drop). Schema-level half of the write-once contract.
9. **Schema idempotency.** Restart the API; on the second lifespan boot `apply_schema()` runs again; pre-existing `requests` rows still have their `capture` values intact (re-run of `ADD COLUMN IF NOT EXISTS` is a no-op). Engine continues to function against existing data.
10. **Double-dispatch defense.** Fire `execute_run(run_id)` twice for the same `run_id` (direct import in a unit test). Assert exactly one terminal UPDATE landed: `finished_at` unchanged across the second invocation, single `run.completed` event row keyed by `target_id`.

AGREE: yes

SCORE: 2

## Architect — Reconciliation

### Resolved

- **`RunRequestResult.captured` is `list[str]`, not `dict[str, str]`** (Data, CRITICAL). The keys-only rule is now enforced by the schema type rather than by engine convention + a separate redaction pass. Engine builds a run-local `captured_dict: dict[str, str]` for the in-process `subs` map; only `sorted(captured_dict.keys())` is persisted. Updated PLAN's Data schema block, the worked-example JSON, the Security mitigations list (item 5), the Security open-concerns list (removed the now-decided question), the Data builder-items list (item 7), and the Data test list (item 3). Mirrored the change in `SPEC.md ## Architect / Pydantic shapes`. Designer's keys-only caption (`SPEC.md:232-233`) needs no change — already aligned.
- **Redaction-pass ordering is explicit and validation runs AFTER redaction** (Data, IMPORTANT). Added a new `#### Redaction-pass ordering (mandatory, in this order)` subsection to `## Data` immediately after the worked-example JSON. Four steps: (a) build raw dict, (b) `_redact_secrets(secret_values_set, result_dict)` mutates url + every header value + body excerpt in place, (c) build `captured: list[str]` from the local dict's keys, (d) `RunRequestResult.model_validate(result_dict)` then `RunEvidence.model_dump(mode="json")` for the `::jsonb` bind. Builder is told validate-AFTER-redact.
- **DEBUG-log fields tightened** (Reliability, NIT). Dropped post-substitution `url` from the DEBUG line — URLs can carry secrets in querystrings. Kept `method`, `host` (hostname only), `status`, `latency_ms`.
- **Collection-size cap is route-level, not a DB constraint** (Data, NIT). Added a row to the integrity table calling out the 50-item cap as enforced in the `POST /v1/runs` handler via a count query; intentionally NOT a check constraint on `collection_items` because operators may author large collections for non-run uses.
- **`_row_to_response` decode-fallback-validate ordering promoted** (Data, NIT). The decode-to-dict-first / fallback-default / then-`model_validate` ordering is now stated next to the schema-validate line in the Pydantic-shape section and echoed in the defensive-read-side subsection, instead of living only in the latter.

### Why

- The `captured` change came from Data (CRITICAL). Security's keys-only rule existed only as a comment + redaction step before; promoting it to a schema type closes the gap mechanically. Security signed off (the open-concern is now decided), no further work for them.
- The redaction-ordering change came from Data (IMPORTANT). Validation before redaction would have left a window where a `RunRequestResult` instance carrying secrets briefly exists, inviting mutation bugs.
- DEBUG-URL tightening came from Security (NIT) reinforced by the redaction-ordering work — if we redact on persist but log raw URLs at DEBUG, we have leaked anyway.
- 50-cap clarification and `_row_to_response` ordering both came from Data (NITs).

### Left as-is

- Security: `AGREE: yes, SCORE: 2` — no architectural drift introduced by the reconciliation. The keys-only schema change strengthens, doesn't relax, the original Security plan. All other Security mitigations (SSRF helper, header sanitizer, secret-value redaction on `url` + `response_headers` + `response_body_excerpt`, no `verify=False`, no twin-overrides in engine, no body/header values in logs) stand unchanged.
- Reliability: `AGREE: yes, SCORE: 3` — no architectural drift. SSRF DNS-lookup latency NIT and redaction-perf NIT were both acknowledged-but-deferred (slice-2 scale tolerates both); they remain acknowledged-but-deferred. The two-acquire connection lifecycle, guarded transitions, 10s per-request timeout, 50-item cap, and Sentry-only-for-unexpected rules are unchanged.
- Manager / Designer: unaffected. The Designer's keys-only caption (`SPEC.md:232-233`) already matches the new schema shape.
