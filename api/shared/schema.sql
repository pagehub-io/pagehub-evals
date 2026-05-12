-- pagehub-evals schema. Applied idempotently on lifespan boot under an
-- advisory lock. Mirrors the resource shape of platform/evals/ with a
-- new `runs` table on top — runs is where the JTBD (verdict-bearing
-- LLM-harness gate) lives. All non-runs tables here are scaffolding;
-- expect refinement when the JTBD pivot lands.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Named config contexts. Variables are plaintext key→value JSON;
-- secrets are Fernet-encrypted strings.
CREATE TABLE IF NOT EXISTS environments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id   TEXT NOT NULL,
    name            TEXT NOT NULL,
    variables       JSONB NOT NULL DEFAULT '{}'::jsonb,
    secrets         JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {key: fernet_ciphertext}
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_user_id, name)
);
CREATE INDEX IF NOT EXISTS environments_owner_idx ON environments (owner_user_id);

-- HTTP request templates. {{VAR}} substitution against the bound
-- environment.
CREATE TABLE IF NOT EXISTS requests (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id   TEXT NOT NULL,
    name            TEXT NOT NULL,
    method          TEXT NOT NULL,
    url             TEXT NOT NULL,
    headers         JSONB NOT NULL DEFAULT '{}'::jsonb,
    body            JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE requests ADD COLUMN IF NOT EXISTS capture JSONB NOT NULL DEFAULT '{}'::jsonb;
CREATE INDEX IF NOT EXISTS requests_owner_idx ON requests (owner_user_id);

-- Per-request response assertions. Multiple per request.
CREATE TABLE IF NOT EXISTS evaluations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id      UUID NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    -- e.g. "status_eq", "json_path_eq", "header_present", "twin_traffic_zero"
    kind            TEXT NOT NULL,
    config          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS evaluations_request_idx ON evaluations (request_id);

-- Ordered groups of requests.
CREATE TABLE IF NOT EXISTS collections (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id   TEXT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_user_id, name)
);
CREATE INDEX IF NOT EXISTS collections_owner_idx ON collections (owner_user_id);

CREATE TABLE IF NOT EXISTS collection_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    collection_id   UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    request_id      UUID NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    position        INTEGER NOT NULL,
    UNIQUE (collection_id, position)
);
CREATE INDEX IF NOT EXISTS collection_items_collection_idx ON collection_items (collection_id);

-- Per-harness API keys. Each LLM agent (or harness installation) gets
-- a key minted by an operator; the secret is hashed at rest (Fernet
-- can't be used here because we need constant-time compare not
-- decrypt). The plaintext is returned to the operator exactly once at
-- creation and never re-rendered.
CREATE TABLE IF NOT EXISTS harness_keys (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    secret_hash     TEXT NOT NULL,                 -- bcrypt or sha256 of the secret
    secret_prefix   TEXT NOT NULL,                 -- first ~6 chars of secret, for "key starting with phk_xyz" hints
    created_by      TEXT NOT NULL,                 -- operator user id who minted it
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at      TIMESTAMPTZ                    -- soft-delete; null = active
);
CREATE INDEX IF NOT EXISTS harness_keys_active_idx ON harness_keys (revoked_at) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS harness_keys_prefix_idx ON harness_keys (secret_prefix);

-- Verdict-bearing executions. The JTBD lives here:
-- * harness_claim    — what the LLM agent said it had done
-- * verdict          — the independent ground-truth call (pass / fail)
-- * evidence         — request/response pairs, twin-traffic counts, timing
-- * created_by_kind  — "user" or "harness_key" (which auth path opened the run)
-- * created_by_id    — corresponding id
-- The agent cannot override `verdict` or `harness_claim`; only the
-- eval engine writes verdict, and harness_claim is write-once at
-- creation time.
CREATE TABLE IF NOT EXISTS runs (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_by_kind    TEXT NOT NULL,              -- "user" | "harness_key"
    created_by_id      TEXT NOT NULL,
    collection_id      UUID REFERENCES collections(id) ON DELETE SET NULL,
    environment_id     UUID REFERENCES environments(id) ON DELETE SET NULL,
    harness_id         TEXT,                       -- e.g. "claude-code-4-7", "cursor-x.y"
    harness_claim      TEXT,                       -- "I fixed the X by doing Y"
    status             TEXT NOT NULL DEFAULT 'pending',  -- pending | running | passed | failed | error
    verdict            TEXT,                       -- passed | failed | error (engine-only)
    evidence           JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at         TIMESTAMPTZ,
    finished_at        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS runs_actor_idx ON runs (created_by_kind, created_by_id);
CREATE INDEX IF NOT EXISTS runs_collection_idx ON runs (collection_id);
CREATE INDEX IF NOT EXISTS runs_status_idx ON runs (status);
CREATE INDEX IF NOT EXISTS runs_created_at_idx ON runs (created_at DESC);

-- Immutable audit trail. Append-only — no UPDATE / DELETE handlers.
-- actor_kind is "user" | "harness_key" | "system".
CREATE TABLE IF NOT EXISTS events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_kind      TEXT NOT NULL,
    actor_id        TEXT,
    kind            TEXT NOT NULL,
    target_kind     TEXT NOT NULL,
    target_id       UUID,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_target_idx ON events (target_kind, target_id);
CREATE INDEX IF NOT EXISTS events_actor_idx ON events (actor_kind, actor_id);
