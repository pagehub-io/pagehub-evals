# SPEC

> Build: pagehub-evals JTBD pivot — finish the `runs/` verdict-bearing engine.

## Manager

### Problem & users

**Who suffers without the verdict gate.** Operators of LLM coding harnesses
(today: us; soon: anyone running long autonomous Claude/Cursor/etc loops).
The agent runs, declares "fixed the bug, deploy is green," and the operator
has no independent way to disprove it without re-running the work by hand.

**What they do today.** Either trust the agent's self-report (sometimes
fiction) or manually `curl` the deployed system. Both cost humans-in-the-loop
on every cycle — the thing the autonomous loop was supposed to remove.

**Cost of doing nothing.** The scaffolded `pagehub-evals` API has the
resources (`environments`, `requests`, `evaluations`, `collections`,
`harness_keys`) but `POST /v1/runs` returns 503 behind a feature flag. The
schema (`api/shared/schema.sql:97`) already carries `harness_claim`,
`verdict`, and `evidence` columns, but nothing populates them. Until the
engine ships, the product's stated JTBD (`CLAUDE.md:3-9`) is unfulfilled
and the harness operator's loop stays manually supervised.

### Users (concrete)

- **LLM harness installations** (auth: harness key) — POST a run with
  `harness_claim`, poll for verdict, hand it back to the agent so the loop
  stops fabricating "done." A harness key sees ONLY its own runs
  (`api/runs/routes.py:137-138`); never another harness's, never an
  operator-created run. This is a security boundary, confirmed scope.
- **Pagehub operators** (auth: pagehub-auth JWT, allowlisted) — read any
  run, inspect evidence on failures, mint/revoke harness keys. List + detail
  views in `mobile/app/(drawer)/runs.tsx` are sufficient this slice.

### Success criteria (this slice)

Measurable, all required:

- `POST /v1/runs` is reachable without a feature flag for both auth kinds;
  returns `202` with the persisted run id.
- A run created against a seeded collection + environment transitions
  `pending → running → {passed|failed|error}` and writes `started_at`,
  `finished_at`, `verdict`, and `evidence` exactly once.
- `harness_claim` is captured verbatim at creation and is byte-identical in
  every subsequent `GET /v1/runs/{id}` response — no code path mutates it
  after insert.
- `PATCH /v1/runs/{id}` returns `403` for operator JWT AND for harness key,
  for every field including `verdict`, `harness_claim`, `status`. The engine
  is the only writer.
- `evidence` JSONB contains, per request: substituted url, response status,
  response headers, response body excerpt, latency_ms, per-eval pass/fail
  with observed-vs-expected detail, and the captured-vars trail used to
  substitute the next request.
- Capture-and-substitute works: a request whose response feeds a JSONPath
  capture (e.g., `$.id → REQ_ID`) makes that value available as `{{REQ_ID}}`
  in any later request's url, headers, or body within the same run.
- All four eval kinds (`status_eq`, `json_path_eq`, `header_present`,
  `body_contains`) contribute pass/fail to the verdict.
- `platform/evals/seeds/pagehub_evals_runs_*.py` exists and exercises:
  create → poll-to-terminal, immutability of `harness_claim`, PATCH-403
  from both auth kinds, capture/substitution across two requests, and one
  failing-eval case that yields `verdict="failed"`. A feature without a
  seed is invisible (`CLAUDE.md:platform/evals/`).

### What "done" looks like vs. slice 3

This slice ends when the four bullets above are green on staging. It does
NOT end with twin-traffic accounting, operator triage charts, or filters
in the mobile UI — those are slice 3.

**Load-bearing edge case:** a `POST /v1/runs` with no `collection_id` (or
a collection with zero items) MUST succeed at the API, run the engine, and
yield `verdict='error'` with `evidence.requests=[]` — the `harness_claim`
is preserved on the row. Rationale: rejecting the POST strands the agent's
claim and invites the agent to rationalize a 4xx as "environment broken,
not my fault." Recording the claim alongside the misconfiguration is the
point of the gate.

### Scope

**In scope**
- Harness-claim ingest (write-once at `POST /v1/runs`).
- Run execution loop in `api/runs/engine.py`: ordered collection items,
  `{{VAR}}` substitution into url/headers/body, JSONPath capture from
  response into the substitution map for subsequent requests.
- Per-request evaluation across the four existing kinds, with structured
  observed-vs-expected detail in `evidence`.
- Terminal verdict written exactly by the engine; PATCH blocked.
- `evidence` JSONB shape: ordered per-request entries with request meta,
  response meta, latency, per-eval outcome, captured-vars trail.
- Mobile: `mobile/app/(drawer)/runs.tsx` shows a list (status + verdict
  badge + harness_id + created_at) and a detail view (harness_claim,
  verdict, per-request evidence).
- Seeds at `platform/evals/seeds/pagehub_evals_runs_*.py` matching the
  slice-1 sibling pattern (`pagehub_evals_evaluations.py`).
- **Cross-repo dependency:** the seed files at
  `~/github/pagehub-io/platform/evals/seeds/pagehub_evals_runs_*.py` are
  AUTHORED in that repo as part of this slice; merging them is a parallel
  PR (in `platform`) and gated by repo etiquette there. Slice is not "done"
  until both PRs (here + platform) are green; the seed PR cannot land
  before the API PR ships to staging.

**Out of scope (this slice — slice 3 owns)**
- Twin-zero-traffic evidence integration (no `twin_traffic_zero` eval kind
  this slice; the schema comment in `api/shared/schema.sql:44` advertises
  it but the engine ignores it).
- Operator triage UX beyond list+detail — no filters, charts, drill-downs,
  search, app/env/harness facets.
- A stale-run reaper for in-process executions that die mid-flight
  (engine docstring at `api/runs/engine.py:10-13` already concedes this).

**Non-goals (we are explicitly not building this slice)**
- Retry-on-failure semantics for failed requests in a run.
- Harness-claim editing endpoints; the field is write-once by design.
- **Raising the `harness_claim` 10 000-char cap** (`api/runs/schemas.py:33`).
  Agents must summarize the claim, not paste a transcript. If a 10 000-char
  cap is genuinely insufficient in practice, revisit in slice 3 with
  evidence; the default position is "summarize."
- Eval-bundle templates or reusable assertion packs.
- Worker-based execution (slice 2 may move off `BackgroundTasks`; not now).
- A second auth dimension (run-level ACLs beyond "harness sees own, operator
  sees all" already implemented at `api/runs/routes.py:137-138`).

### What of the WIP prototype we keep

The uncommitted WIP (`api/runs/engine.py`, `api/runs/routes.py`,
`api/runs/schemas.py`) already implements most of this slice cleanly:
substitution walker, JSONPath-lite resolver, all four eval kinds, verdict
aggregation, PATCH-403 guard, harness/operator read-scoping. The
architect/builder has license to keep that surface and focus net-new
effort on: removing the feature flag, adding **JSONPath capture from
response → substitution map** (currently substitution is environment-only;
the brief calls for per-response capture too), the seed file, and the
mobile screens.

### Risks & invalidation signals

1. **Capture-rule schema location — MITIGATED.** Architect adopted
   Option 1: add `requests.capture JSONB NOT NULL DEFAULT '{}'::jsonb`
   (one-line idempotent `ADD COLUMN IF NOT EXISTS` in
   `api/shared/schema.sql`). Captures hang off the request template, which
   matches the seed pattern at
   `~/github/pagehub-io/platform/evals/seeds/_shared.py:151`. Authoring UI
   for `capture` is **slice 3**; slice 2 ships API-only access and the
   seed file uses the API directly. No live risk on this point.

2. **In-process `BackgroundTasks` execution makes runs untestable from
   evals.** The seed loops would have to poll until `status` leaves
   `pending|running`. If the staging worker recycles between request and
   poll, the run is orphaned and the seed flakes.
   *Invalidation signal:* >1% flake rate on the runs seed in staging CI.
   That kicks slice 2 (worker-based runner) forward.

3. **`verdict="error"` semantics are too broad.** Current aggregation
   (`api/runs/engine.py:321-326`) returns `error` for *any* request-level
   transport error AND for the no-evaluations case. Operators can't tell
   "the system under test is down" from "I forgot to add evals." That
   muddies the gate the JTBD relies on.
   *Invalidation signal:* on the first 20 real runs, operators ask "why
   did this error?" more than once. Tighten the distinction in slice 3.

AGREE: yes

## Designer

Scope of this slice: **operator-facing read-only verdict surface**. The write
surface is `POST /v1/runs` (harness-key actor) — no operator UI for that; the
operator only observes. Two screens, one shape change to the existing
`ThemeContext`.

### Operator flows

#### 1. Run list — `mobile/app/(drawer)/runs.tsx` (replace stub)

Reuse the existing list-screen idiom in `mobile/app/(drawer)/environments.tsx`
and `requests.tsx` (ScrollView + theme tokens + `createStyles(theme)`). On
mount, `GET /v1/runs` via `mobile/services/api.ts`, render newest-first.

Row contents (one tappable card per run):

- **Top row (left → right):** verdict pill (see below) · `harness_id` (or
  `"—"` if null) · created_at as relative time (`"3m ago"`, `"yesterday"`).
- **Middle:** `harness_claim` truncated to **2 lines, ellipsis** (the full
  multi-paragraph blob lives on the detail screen). If null, render muted
  `"(no claim recorded)"`.
- **Bottom row (only when `status in {passed, failed, error}`):** request
  count (`evidence.requests.length`) · total duration
  (`finished_at − started_at`, rendered like `"1.2s"` / `"340ms"`). Hidden
  for `pending` / `running` — those rows show a "running" pill instead and
  no summary line.

Tap row → `router.push('/runs/' + id)`.

Refresh: pull-to-refresh on the list. No auto-poll on the list screen this
slice (auto-poll is detail-only — see below).

#### 2. Run detail — `mobile/app/(drawer)/runs/[id].tsx` (new file)

Expo Router dynamic route. On mount, `GET /v1/runs/{id}`. Layout:

- **Header block** (sticky at top of scroll):
  - Verdict pill (large variant).
  - `harness_id` as monospace caption.
  - Timing: `started_at` (absolute) + total duration. If still running,
    show elapsed since `started_at` and a subtle activity indicator next
    to the pill — no full-screen spinner; the page already has data.
- **Harness claim block:**
  - Section header `"Harness claim"`.
  - Full `harness_claim` text in a bordered surface card
    (`theme.colors.surface` background, `theme.colors.border` 1px).
    Preserve newlines (`<Text>` respects `\n` in React Native). No
    truncation here.
  - If null: muted `"(no claim recorded)"`.
- **Request results list** (one card per entry in `evidence.requests`, in
  execution order):
  - **Row 1:** `request_name` (bold) · per-request status glyph derived
    directly from server-computed `RunRequestResult.passed`:
    - `✓` if `passed === true`.
    - `✕` if `passed === false` AND `transport_error` is null.
    - `⚠` if `transport_error` is set (distinct from eval failure).
  - **Row 2 (monospace, muted):** `method` · `url` — the
    **post-substitution** URL the engine actually fired. Per-row warning
    indicator (small `⚠` after the URL, `colors.warning`) when this
    request's `substitution_missed` is non-empty.
  - **Row 3:** colored status-code chip (see semantics) · latency
    (`"234ms"`). If `transport_error` is set, render the error string
    here in place of the status+latency chip (e.g. `"ReadTimeout: …"`).
  - **Captured caption (muted, only when `captured` is non-empty):** a
    single line `"captured: CREATED_ID, AUTH_TOKEN"` — keys only, comma-
    separated, in insertion order. Values-on-tap is deferred to slice-3.
  - **Evaluations sub-list** (indented, one row per entry in
    `result.evaluations`):
    - ✓/✕ icon · `name` · `kind` as muted caption · short detail line
      derived from the eval payload:
      - `status_eq` → `"expected 200, got 404"`
      - `json_path_eq` → `"$.user.id: expected 'abc', got 'xyz'"`, or
        `"$.user.id: missing"` when `missing: true`.
      - `header_present` → `"X-Request-Id: present"` / `"missing"`.
      - `body_contains` → `"contains 'ok': true"` / `"false"`.
      - Unknown kind / engine error on eval → red `error` string verbatim.

Auto-refresh: when `status in {pending, running}`, poll `GET /v1/runs/{id}`
every **2s**. Stop polling on first terminal status. Stop polling after
**5 minutes of continuous `running` status** (matches the future stale-run
reaper window); at that point show a muted caption `"still running…"` and a
manual **Retry** button — pressing Retry resumes 2s polling for another 5
minutes. Keep pull-to-refresh enabled at all times — operators expect to
force a refresh themselves.

Read-only: no buttons, no destructive actions. PATCH is server-rejected
anyway; the UI just doesn't offer it.

### Empty / loading / error states

| Condition | UI |
|---|---|
| Run list, initial fetch in flight | Single centered `ActivityIndicator`, no skeleton rows. Match the bare style of the existing stub screens. |
| Run list, fetch succeeds with `items: []` | Centered block: title `"No runs yet"`, body `"Runs appear here when a harness POSTs to /v1/runs with its claim."` — mirrors the copy density of the current stubs. |
| Run list, fetch errors (network / 5xx) | Centered block: `"Couldn't load runs"` · the error message in muted text · a `"Retry"` text button (TouchableOpacity, primary color). No silent retry. |
| Run list, 401/403 | Same shape, copy `"Not authorized to view runs"`. No retry button. |
| Run detail, initial fetch in flight | Centered `ActivityIndicator`. |
| Run detail, 404 | Centered block: `"Run not found"`, body `"This run id doesn't exist or you don't have access."`, "Back to runs" link → `/runs`. |
| Run detail, run is `pending` | Header pill = "pending" (muted). Body: muted line `"Waiting for engine to pick up this run."` No request list (evidence is empty). |
| Run detail, run is `running` | Header pill = "running" (primary). Body: muted line `"Engine is executing requests. This page refreshes every 2 seconds."` Request list renders whatever requests are already in evidence (engine writes terminal-only today; UI tolerates either). |
| Run detail, terminal `error` (engine itself failed, not just a per-request error) | Verdict pill = "error" (warning color). Below header, a warning-bordered surface card titled `"Engine error"` showing `evidence.engine_error` if present, else `"Engine terminated with status=error but did not record details."` Request list still renders if `evidence.requests` is non-empty (partial progress). |
| Detail polling fetch fails mid-run | Keep last good state on screen; show small muted footer `"Refresh failed, retrying…"`. Don't blank the page. After 3 consecutive failures, stop polling and surface a `"Retry"` button. |

### Verdict pill semantics

Pills are small rounded rects with text. Five states; the current theme
exposes no semantic color tokens, so this slice **adds three** to
`mobile/contexts/ThemeContext.tsx`. Flagging explicitly per the prompt:

> **Theme token change required.** Existing `Theme.colors` is
> `background, surface, text, textMuted, primary, border` only
> (`mobile/contexts/ThemeContext.tsx:9-17`). Add `success`, `danger`,
> `warning`. Suggested light-theme values: `success: '#1a7f37'`,
> `danger: '#cf222e'`, `warning: '#9a6700'` (GitHub Primer palette,
> matches `primary: '#1f6feb'` already in the file).
> If the architect wants tokens frozen this slice, the fallback is:
> passed → `primary`, failed/error → `text` with the glyph carrying
> meaning. Strongly prefer adding the tokens — a verdict surface that
> doesn't visually distinguish pass from fail defeats the JTBD.

| State | Pill background | Pill text | Glyph |
|---|---|---|---|
| `passed` | `colors.success` (fallback: `primary`) | white | `✓` |
| `failed` | `colors.danger` | white | `✕` |
| `error` | `colors.warning` | white | `!` |
| `running` | `colors.primary` | white | small spinner inline |
| `pending` | `colors.surface` + 1px `colors.border` | `colors.textMuted` | `·` |

Status-code chips on request rows use the same palette: `2xx → success`,
`3xx → primary`, `4xx → warning`, `5xx → danger`. `response_status=0` is
the engine's sentinel for "request errored before a response" — render
it as the literal string `"ERR"` (not `"0"`), `danger` colored, with the
`transport_error` field shown next to it.

### Out of scope this slice (deferred to slice-3 — do NOT build)

- Filters (by app / environment / harness / status / verdict).
- Search box over `harness_claim`.
- Harness drill-in (`/harnesses/[id]` aggregate view).
- Re-run / retry button on the detail screen.
- Evidence raw-JSON download / copy-evidence-as-JSON action.
- Twin-zero-traffic evidence panel (the "did service A leak a call to
  service B" assertion). Schema may carry it later; UI ignores it this
  slice.
- Run-to-run diffing.
- Pagination — `GET /v1/runs` already caps at 500; pagination ships with
  filters in slice 3.
- Long-press / clipboard helpers on `harness_id` and other ids.
- Capture-rule editor UI (authoring `requests.capture`) — slice-2 ships
  only API access; mirrors the architect's deferred list.
- Values-on-tap for the per-request `captured` keys (only keys render this
  slice).

### Edge cases the design must handle

- **Long `harness_claim` (multi-paragraph, code blocks, up to 10 000
  chars per `api/runs/schemas.py:33`):** detail screen renders the full
  string with `\n` preserved. No markdown rendering this slice — raw
  text in a surface card. List screen always truncates to 2 lines
  regardless of content.
- **Null `harness_claim` / null `harness_id`:** list shows muted
  placeholder text (`"(no claim recorded)"` / `"—"`). Detail shows the
  same placeholder in the claim block; the page still renders.
- **Non-JSON or odd response body (`response_body_excerpt` is a string,
  possibly HTML/text):** render as monospace text, truncated to the
  engine's already-applied 1000-char excerpt; no pretty-print attempt.
  If `response_body_excerpt` is a dict/list, render the first 200 chars
  of `JSON.stringify(body)` inline followed by `…`. No expander control
  this slice — operators can re-fire the request from a real tool if
  they need the full body.
- **Substitution miss — `{{VAR}}` left literal in url/headers/body:**
  consume `RunRequestResult.substitution_missed` directly from the
  server — list the missed keys (e.g. `"unresolved: CREATED_ID,
  AUTH_TOKEN"`) in the inline warning above the request list whenever
  any request has non-empty `substitution_missed`; per-row warning
  indicator on Row 2 (spec'd above) when that request's
  `substitution_missed` is non-empty. No client-side regex — the engine
  is authoritative and covers headers/body that a URL-only regex would
  miss.
- **Per-request transport error (network/timeout — engine writes
  `transport_error` string, `response_status=0`, no evaluations run):**
  render the row with `⚠`, show `transport_error` verbatim where
  status+latency would go, omit the evaluations sub-list (it's empty
  anyway). Run-level verdict is `error` per engine aggregation.
- **`evidence.requests` is `[]`** (collection had no items, or
  `collection_id` was null): detail screen shows the header block
  normally, then a muted `"No requests executed."` line where the list
  would be. Verdict will be `error` (engine sets verdict=error when
  `not any_evaluations`); the header pill already conveys that.
- **Concurrent runs of the same collection:** each gets its own row; no
  merging. List is strictly `created_at DESC`.
- **Clock skew on `started_at` / `finished_at` (Postgres `now()` across
  conns):** if `finished_at < started_at`, render duration as `"<1ms"`
  rather than a negative number.
- **Very fast run (sub-ms):** render `"<1ms"`, not `"0ms"`, so the
  operator doesn't think the run didn't actually fire.
- **Operator viewing a harness-created run, harness viewing operator-
  created run:** auth already enforced server-side (`routes.py:137-138`
  — harness keys 403 on other-actor runs). UI treats 403 the same as
  404 on the detail screen (`"Run not found"`) — don't leak existence.

AGREE: yes

## Architect

### WIP triage verdict

- `api/runs/routes.py` — **REFACTOR (keep ~90%).** Auth split, 503 gate, 202 + background dispatch, harness-key self-read clamp, and blanket PATCH 403 are correct. Slice-2 ships with `RUNS_ENABLED=true` everywhere so leaving `_gate()` on all five handlers is fine. One small edit: `_row_to_response` (routes.py:36-54) currently constructs `RunResponse` from a raw asyncpg row; **`RunResponse.evidence` type swaps from `dict[str, Any]` to `RunEvidence`** (see Contracts), so this needs to round-trip through `RunEvidence.model_validate(row["evidence"])` before constructing `RunResponse`. PLAN's data section will line-item this validation flow.
- `api/runs/schemas.py` — **KEEP, EXTEND.** `RunStatus` and `RunVerdict` enums are correct. `CreateRunRequest` correctly omits `verdict`/`status`/`evidence` (write-once at engine), `RunResponse` shape matches the row. Add `model_config = ConfigDict(extra="forbid")` on `CreateRunRequest` so a client attempting to send `verdict` at POST time gets 422 rather than silent drop — this is the HTTP-layer enforcement of write-once. Add `RunRequestResult`, `RunEvaluationResult`, `RunEvidence` (see Contracts).
- `api/requests/schemas.py` + `api/requests/routes.py` — **EXTEND.** Add `capture: dict[str, str] = {}` to `CreateRequestRequest`, `UpdateRequestRequest`, and `RequestResponse`. Routes' INSERT/UPDATE/SELECT statements must read/write the new `requests.capture` JSONB column. Required by the capture-and-substitute success criterion; the slice-1 seed at `pagehub_evals_evaluations.py:63` already POSTs `capture={"PARENT_REQ_ID": "$.id"}` and the schema must accept it.
- `api/runs/engine.py` — **REFACTOR.** The bones are right, the joints leak. Per concern:
  - **Substitution walker** (`_substitute`, engine.py:35-49): **KEEP.** Pure recursion over str/dict/list, leave-on-miss matches policy. NIT: O(N*K) string scans; fine for slice-2 sizes (<50 vars, <50 requests).
  - **`_resolve_path`** (engine.py:60-98): **KEEP.** JSONPath-lite handles `$.a.b[0]` and chained `$.a[0].b` correctly via the `_MISSING` sentinel. Bracket-quoted keys (`$['a.b']`) are unsupported — document inline, punt to slice-3 if anyone needs it.
  - **Eval-kind dispatch** (engine.py:129-134, `_KINDS`): **KEEP.** Table-driven, clean to extend. Slice-3 adds `twin_traffic_zero` here.
  - **Verdict aggregation** (engine.py:316-331): **REFACTOR.** Current `all_eval_passed` is computed across ALL per-request evals globally — fine, but a per-request `transport_error` does NOT short-circuit that request's evals. Restate so each request has a clean `passed` boolean and the run-level rule reads off those. See Verdict Aggregation below.
  - **Evidence shape** (engine.py:222-244, 333): **REFACTOR.** Wraps as `{"requests": [...]}` which is right; per-request entry is missing `substitution_missed: list[str]` (required by miss-policy). Add it. `body_excerpt` deep-copy via `json.loads(json.dumps(...))` (engine.py:227) is wasteful — drop the deep-copy and trust `r.json()` returned fresh objects.
  - **Transaction boundaries** (engine.py:250-353): **REFACTOR.** Currently holds a single `pool.acquire()` connection across the whole run including N HTTP fires. With a 10-conn pool (`api/shared/db.py:18`) and `command_timeout=30` that's pool-starvation risk for any run >30s. Reshape: acquire conn #1 to write `started_at`/`status='running'`, RELEASE before HTTP fires, acquire conn #2 for the terminal UPDATE + final events. Engine should call `get_pool()` and manage its own short-lived acquires, not hold one connection for the run lifetime.
  - **In-process `BackgroundTasks`** (routes.py:96): **ACCEPTABLE FOR SLICE-2 with one guardrail.** It's per-uvicorn-worker, non-durable, dies on deploy/crash. Slice-2 caveat: every terminal UPDATE must be guarded `WHERE status='running'` so a re-fire on the same `run_id` can't clobber an already-written verdict (the engine writes terminal state only after the `pending → running` transition succeeded, never directly from `pending`). The stale-run reaper is slice-3 — name it in PLAN so reliability flags the gap rather than rebuilding it now.

### Components

- **`api/runs/routes.py`** — HTTP only. Auth resolution, body validation, row serialization, dispatch into `BackgroundTasks`. No HTTP-client logic, no substitution, no evaluation. Imports from `api.runs.engine` are limited to the `execute_run` entry point.
- **`api/runs/engine.py`** — Pure execution. **MUST NOT** import `fastapi.*` (no `Request`, `BackgroundTasks`, `HTTPException`, `Depends`, no Pydantic request/response models — engine returns dicts that the route serializes). Talks to Postgres via `asyncpg` through `api.shared.db.get_pool()`, fires outbound HTTP via `httpx.AsyncClient`. Public surface is exactly one coroutine: `async def execute_run(run_id: UUID) -> None`. Everything else (`_substitute`, `_resolve_path`, `_KINDS`, `_execute_request`, `_apply_captures`) stays module-private.
- **`api/environments/substitution.py`** — **NEW (slice-2 prerequisite).** Extract `load_substitution_map(db, environment_id) -> dict[str, str]` out of `api/environments/routes.py:188-202` into this non-FastAPI module so the engine can import it without dragging FastAPI symbols transitively. `environments/routes.py` updates its single in-file caller to import from the new module. No back-compat shim — internal helper, single-repo refactor. The current engine import (`api/runs/engine.py:25` → `api.environments.routes`) is what makes the "engine MUST NOT import fastapi" rule false today; this is the fix.
- **`api/runs/schemas.py`** — Pydantic only. Existing enums + `CreateRunRequest`/`RunResponse`/`RunListResponse`, plus new `RunRequestResult`, `RunEvaluationResult`, `RunEvidence` for typed evidence. No DB, no FastAPI types.

### Integrations

Internal helpers the engine consumes (all in-repo, all non-FastAPI):

- **`api.environments.substitution.load_substitution_map(db, environment_id) -> dict[str, str]`** — new module (see Components). Returns `{**variables, **decrypted_secrets}` for the run's environment. Engine seeds the run-local substitution map from this single call at run start.
- **`api.shared.db.get_pool()` / `get_db()`** — asyncpg connection pool. Engine acquires short-lived connections per the Concurrency rules; never holds across HTTP fires.
- **`api.shared.events.record_event(...)`** — emits `run.created` / `run.started` / `run.completed` rows into the shared `events` table. The audit timeline is addressable via the existing `GET /v1/events?target_kind=run&target_id={id}` endpoint; no nested `/runs/{id}/events` route is added.
- **`api.dependencies.{require_auth, require_user}`** — auth resolution. Routes use these; engine does not (engine runs without an auth context, dispatched via `BackgroundTasks`).

Cross-repo dependency (parallel PR, gated by the same slice):

- **`~/github/pagehub-io/platform/evals/seeds/pagehub_evals_runs_*.py`** — authored as part of this slice but lands as a PR in the `platform` repo. Slice is not "done" until both PRs (this repo + `platform`) are green; the seed PR cannot land before the API PR ships to staging. The seed exercises create → poll-to-terminal, PATCH-403 from both auth kinds, capture/substitution across two requests, and one failing-eval case. The pre-existing slice-1 seed at `platform/evals/seeds/pagehub_evals_evaluations.py:63` already POSTs `capture={"PARENT_REQ_ID": "$.id"}` against `POST /v1/requests`; the `capture` field added to `api/requests/{schemas,routes}.py` MUST accept this shape unchanged or the slice-1 seed regresses.

### Contracts

#### HTTP

- `POST /v1/runs` — `require_auth` (user JWT OR `X-Harness-Key`; both → 422 via the existing dep at `api/dependencies.py:117-121`). Body `CreateRunRequest` (`extra="forbid"`). Returns **202** + `RunResponse` with `status="pending"`, `verdict=null`, `evidence={"requests": []}`. Dispatches `execute_run(row.id)` via `BackgroundTasks`. A client attempting to send `verdict` or `status` in the body → 422 (extra fields forbidden).
- `GET /v1/runs` — `require_user` (operator-only; harness keys 403). Newest first, `LIMIT 500`. Pagination deferred to slice-3.
- `GET /v1/runs/{id}` — `require_auth`. Operator sees any run; harness key sees only rows where `created_by_kind='harness_key' AND created_by_id = auth.actor_id` (matches existing routes.py:137-138). Mismatched harness key → **403** (designer treats 403 same as 404 in UI to avoid leaking existence; backend remains 403 so eval seeds can distinguish "not yours" from "doesn't exist").
- `PATCH /v1/runs/{id}` — `require_auth`, always **403**. No body parsing; route exists solely to make the contract explicit and testable from both auth kinds.
- **No `GET /v1/runs/{id}/events`.** Recommendation: the `evidence` JSON is sufficient for run-detail. The `events` table is shared infra (already queryable via `GET /v1/events?target_kind=run&target_id={id}`) and emits `run.created` / `run.started` / `run.completed` records — the audit timeline is already addressable. Mobile run-detail reads `evidence.requests[]` for per-request rows and (optionally, slice-3) reuses the existing events endpoint for the timeline. Adding a nested route duplicates that surface.

#### Pydantic shapes (additions to `schemas.py`)

```python
class RunEvaluationResult(BaseModel):
    id: UUID
    name: str
    kind: str
    passed: bool
    detail: dict[str, Any] = {}          # observed/expected/path/etc
    error: str | None = None             # python exception during eval

class RunRequestResult(BaseModel):
    request_id: UUID
    request_name: str
    method: str
    url: str                              # post-substitution
    response_status: int                  # 0 if transport-errored
    response_headers: dict[str, str]
    response_body_excerpt: Any            # str|dict|list, truncated server-side
    latency_ms: int
    transport_error: str | None           # None if HTTP completed
    substitution_missed: list[str]        # keys present as {{X}} that subs lacked
    captured: list[str]                   # keys only — vars this request added to the run's sub map; values live in the in-process subs dict, never persisted
    evaluations: list[RunEvaluationResult]
    passed: bool                          # transport_error is None AND every eval passed

class RunEvidence(BaseModel):
    requests: list[RunRequestResult]
    # engine_error reserved for slice-3 (broader error taxonomy)

# RunResponse.evidence: RunEvidence   (was dict[str, Any])
```

This makes `/docs` accurate and gives the mobile client a typed shape matching the designer's per-request row contract.

### {{VAR}} substitution + capture contract

- **Source of vars (slice-2):** `api.environments.substitution.load_substitution_map(db, environment_id)` returns `{**variables, **decrypted_secrets}`. Engine seeds the run's substitution map with this dict.
- **Substitution miss policy:** leave the literal `{{VAR}}` in the outgoing string (already implemented at `engine.py:40-44`). Engine tracks missed keys per request by serializing url+headers+body (`json.dumps(...)` on the post-substitution payload) and scanning with the **authoritative regex** `re.findall(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}", rendered_serialized)`. The captured names (deduplicated, order-preserved) become `RunRequestResult.substitution_missed`. Misses do NOT raise; they DO contribute to per-request failure only when an eval's expected value depends on the substituted value (no special "missing-var" error path). **UI consumes `substitution_missed` directly** — no client-side regex; the Designer's note about coloring `{{…}}` matches in the URL pulls from this field, not from re-parsing the rendered string.
- **Captured vars (chaining `$.id` → `CREATED_ID` across requests):** the `requests` table has no `capture` column today (`api/shared/schema.sql:25-35`). Three options considered:
  1. Add `requests.capture JSONB NOT NULL DEFAULT '{}'::jsonb` — body `{"CREATED_ID": "$.id"}`. Engine applies post-`_execute_request`, writes the result into `RunRequestResult.captured` and into the run-local `subs` map for subsequent requests.
  2. Store captures on `collection_items.capture JSONB` — same request reused in different collections with different captures.
  3. Punt capture this slice; document the gap; single-request claims (deploy + health check) still work.
- **Recommendation: Option 1** (add to `requests`). Capture is a property of the request template, not its position. The reference seed harness in `~/github/pagehub-io/platform/evals/seeds/_shared.py:151` and the per-feature seed at `pagehub_evals_evaluations.py:63` already encode `capture={"PARENT_REQ_ID": "$.id"}` at request level — hanging captures elsewhere is inconsistent with the system we're modeling on. The schema bump is one `ADD COLUMN IF NOT EXISTS` line in `api/shared/schema.sql`, idempotent under the existing lifespan apply. **Authoring UI for `capture` is slice-3**; slice-2 ships only API access (and the seed file uses the API directly).

### Verdict aggregation

Per request (computed in engine, written into `RunRequestResult.passed`):

```
request.passed = (transport_error is None)
              AND (evaluations is non-empty)
              AND all(ev.passed for ev in evaluations)
```

Per run (single terminal UPDATE):

```
if any(r.transport_error for r in requests):
    verdict = "error"
elif not any(r.evaluations for r in requests):     # nothing was actually checked
    verdict = "error"
elif all(r.passed for r in requests):
    verdict = "passed"
else:
    verdict = "failed"

status = verdict                                    # status mirrors verdict on terminal transition
```

While in flight: `status='running'`, `verdict IS NULL`. The `status='pending'` window is the gap between INSERT and the engine's first UPDATE — typically <100ms but a real state and reported as-is.

Note for manager (cross-section): manager's risk #3 (verdict='error' is too broad) is acknowledged but NOT split this slice — splitting "transport error" from "no evals defined" into distinct verdicts (`error` vs `misconfigured`) is a slice-3 schema bump.

### Concurrency / execution model

- **Slice-2:** `FastAPI BackgroundTasks` (in-process, per-uvicorn-worker, no durability). Acceptable for runs <30s end-to-end, which is the target shape (a handful of HTTP fires against a deployed system).
- **Hard rules (engine.py):**
  1. The row's `status='pending'` is written by the route in the same INSERT that creates the row (routes.py:72).
  2. Engine acquires a connection, writes `status='running', started_at=now()` guarded `WHERE id=$1 AND status='pending'`. If 0 rows updated, abort silently (someone already started this run; re-fire defense).
  3. Engine RELEASES the connection before any HTTP fire.
  4. Engine fires all requests with NO DB connection held. **REQUIRED:** fetch all `requests` rows + their `evaluations` rows up front in one batched read (single connection, released before the first HTTP fire). No per-request DB acquires during the HTTP loop. This is the rule, not a preference — it bounds connection-pool pressure regardless of run length.
  5. Engine re-acquires a connection for ONE atomic terminal UPDATE: `UPDATE runs SET status=$1, verdict=$2, evidence=$3::jsonb, finished_at=now() WHERE id=$4 AND status='running'`. Guard prevents double-write if a duplicate background dispatch fires on the same `run_id`.
- **Worker-death gap (named, NOT built this slice):** a uvicorn restart mid-run leaves the row in `status='running'` forever. **Reliability must flag this in PLAN.** Slice-3 builds a reaper (`UPDATE runs SET status='error', verdict='error' WHERE status='running' AND started_at < now() - interval '5 minutes'`).

### Idempotency & write-once

- `harness_claim`: written exactly once at INSERT (routes.py:67-83). No UPDATE path touches it. PATCH → 403 universally (routes.py:145-154). Eval seed asserts PATCH 403 from BOTH operator JWT and harness key.
- `verdict`: engine-only. Terminal UPDATE in engine.py is the sole writer. Guard clause `WHERE status='running'` prevents re-writes from a stale background dispatch on the same `run_id`. (Narrower than `IN ('pending','running')` because the engine writes terminal only after the `pending → running` transition succeeded; matches Concurrency rule 5.)
- `status`: engine-only after INSERT. Routes never UPDATE status.
- `evidence`: engine-only at terminal UPDATE. Stays `{"requests": []}` between INSERT and terminal write.
- `created_by_kind` / `created_by_id`: set from `AuthContext` at INSERT, immutable thereafter (no UPDATE path).

### Mobile architecture

- **`mobile/app/(drawer)/runs.tsx`** — replace the stub. Run-list per designer spec. Server-side sort newest first; no client-side filtering this slice.
- **`mobile/app/(drawer)/runs/[id].tsx`** — new dynamic route under the existing drawer group (designer's path; sits as a stack route off the drawer's `/runs`). Reads `GET /v1/runs/{id}`; auto-polls every 2s while `status in {pending, running}`.
- **Data flow:** extend `mobile/services/api.ts` with `listRuns()`, `getRun(id)`, `createRun(body)`. Mirror server schemas as TypeScript interfaces (`Run`, `RunRequestResult`, `RunEvaluationResult`, `RunEvidence`). Strict mode, no `any` — types match `RunResponse` exactly. No new abstraction layer.
- The Designer's `ThemeContext` token additions (`success`/`danger`/`warning`) are NOT architectural — pass through.

### Deferred to slice-3 (named so reviewers do not flag)

- **Twin-zero-traffic evidence** — new eval-kind `twin_traffic_zero` slotting into `_KINDS`, plus harness-side assertion that the real dependency saw zero traffic for the request (per `CLAUDE.md` "Eval assertion" rule).
- **Stale-run reaper** — `UPDATE runs SET status='error', verdict='error' WHERE status='running' AND started_at < now() - interval '5 minutes'`; Modal scheduled job or pg_cron.
- **Operator triage UX** — filters (app, harness, status), search by `harness_claim` substring, harness drill-in (group by `harness_id`), re-run button.
- **Capture-rule editor UI** — slice-2 ships only API access to `requests.capture`; seeds use the API directly.
- **Verdict taxonomy split** — separating "transport error" from "no evaluations defined" into distinct verdicts (addresses manager risk #3).
- **Pagination on `GET /v1/runs`** — currently fixed `LIMIT 500`.
- **Durable execution** — swap `BackgroundTasks` for Modal worker.
- **Bracket-quoted JSONPath keys** (`$['a.b']`) in `_resolve_path`.

### Resolutions

- **Harness self-read scope:** own-runs-only (current `routes.py:137-138` behavior stays; harness keys see only rows they created).
- **`harness_claim` size cap:** 10 000 chars stays (agents summarize at the boundary).
- **`collection_id IS NULL` run semantics:** route accepts the POST; engine runs with zero requests; verdict = `error` with `evidence.requests = []` and `harness_claim` preserved.
- **Capture-rule schema location:** Option 1 — add `requests.capture JSONB NOT NULL DEFAULT '{}'::jsonb` to `api/shared/schema.sql`, surfaced in `api/requests/{schemas,routes}.py`. Authoring UI is slice-3.

AGREE: yes
