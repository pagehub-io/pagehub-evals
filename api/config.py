"""Application configuration from environment variables.

Strict mode: every variable below MUST be set explicitly. The app
refuses to boot if any required value is missing — across ALL
environments, including development. No silent defaults, no "helpful"
dev fallbacks. This repo is public, and the cost of a wrong silent
default is much higher than the cost of an explicit boot error.

Two and only two values are optional (absence is meaningful, not a
default):
- ``SENTRY_DSN`` — absence disables Sentry; presence enables it.
- ``SENTRY_TRACES_SAMPLE_RATE`` — only consulted when SENTRY_DSN is set.
"""

import logging
import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

load_dotenv(".env.local")
load_dotenv()

logger = logging.getLogger(__name__)

# Hardcoded app identity. The deploy workflow pushes APP_SLUG=<app_name>;
# we assert that value equals this constant so a mis-deploy
# (pagehub-evals code shipped under the wrong Vercel project name)
# fails closed rather than silently impersonating another app.
EXPECTED_APP_SLUG = "pagehub-evals"


class ConfigurationError(Exception):
    pass


def _require(name: str) -> str:
    """Return env var ``name`` or raise ``ConfigurationError``.

    Treats unset, empty, and whitespace-only as missing. No defaults.
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        raise ConfigurationError(
            f"{name} is required and must be set in the environment (no default)"
        )
    return raw.strip()


def _require_bool(name: str) -> bool:
    raw = _require(name).lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(
        f"{name} must be a boolean (true/false), got {raw!r}"
    )


def _parse_signing_keys_env(raw: str) -> list[tuple[str, str]]:
    """Parse JWT_SIGNING_KEYS into [(kid, secret), ...].

    Format: comma-separated ``kid:secret`` pairs, e.g.
    ``kid-1:secret-1,kid-2:secret-2``. Entries without a ``:`` are
    rejected — no synthesized 'default' kid, no implicit single-secret
    fallback. Either supply at least one ``kid:secret`` pair, or the
    process refuses to boot.
    """
    keys: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise ConfigurationError(
                "JWT_SIGNING_KEYS entries must be in 'kid:secret' format"
            )
        kid, secret = entry.split(":", 1)
        kid = kid.strip()
        secret = secret.strip()
        if not kid or not secret:
            raise ConfigurationError(
                "JWT_SIGNING_KEYS entries must have non-empty kid and secret"
            )
        keys.append((kid, secret))
    if not keys:
        raise ConfigurationError("JWT_SIGNING_KEYS must contain at least one kid:secret pair")
    return keys


@dataclass(frozen=True)
class Settings:
    env: str
    git_sha: str
    app_url: str
    pagehub_auth_base_url: str
    pagehub_auth_issuer: str
    service_api_key: str
    jwt_signing_keys: list[tuple[str, str]]
    app_slug: str
    runs_enabled: bool
    database_url: str
    encryption_key: str
    admin_emails: frozenset[str]
    sentry_dsn: Optional[str]
    sentry_traces_sample_rate: Optional[float]


_settings_singleton: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings_singleton
    if _settings_singleton is not None:
        return _settings_singleton

    # Required across all environments — no fallbacks.
    env = _require("ENVIRONMENT")
    git_sha = _require("GIT_SHA")
    app_url = _require("APP_URL").rstrip("/")
    pagehub_auth_base_url = _require("PAGEHUB_AUTH_BASE_URL").rstrip("/")
    pagehub_auth_issuer = _require("PAGEHUB_AUTH_ISSUER")
    service_api_key = _require("SERVICE_API_KEY")
    app_slug = _require("APP_SLUG")
    if app_slug != EXPECTED_APP_SLUG:
        raise ConfigurationError(
            f"APP_SLUG={app_slug!r} but this code is {EXPECTED_APP_SLUG!r}; "
            f"refusing to boot under the wrong app identity"
        )
    database_url = _require("DATABASE_URL")
    encryption_key = _require("ENCRYPTION_KEY")
    runs_enabled = _require_bool("RUNS_ENABLED")

    admin_emails = frozenset(
        e.strip().lower()
        for e in _require("ADMIN_EMAILS").split(",")
        if e.strip()
    )
    if not admin_emails:
        raise ConfigurationError(
            "ADMIN_EMAILS must contain at least one email address"
        )

    # JWT_SIGNING_KEYS is the canonical multi-key form (rotation
    # overlap). The platform deploy workflow currently pushes the
    # singular JWT_SIGNING_KEY for compat — accept either, but require
    # one of them, and demand kid:secret in both cases.
    raw_signing = os.getenv("JWT_SIGNING_KEYS", "").strip()
    if not raw_signing:
        single = os.getenv("JWT_SIGNING_KEY", "").strip()
        if not single:
            raise ConfigurationError(
                "JWT_SIGNING_KEYS (or JWT_SIGNING_KEY) is required (no default)"
            )
        if ":" not in single:
            raise ConfigurationError(
                "JWT_SIGNING_KEY must be in 'kid:secret' format (no synthesized kid)"
            )
        raw_signing = single
    signing_keys = _parse_signing_keys_env(raw_signing)

    # Optional only — absence disables Sentry; presence enables it.
    sentry_dsn = os.getenv("SENTRY_DSN", "").strip() or None
    sentry_traces_sample_rate: Optional[float] = None
    if sentry_dsn:
        raw_rate = os.getenv("SENTRY_TRACES_SAMPLE_RATE", "").strip()
        if raw_rate:
            try:
                sentry_traces_sample_rate = float(raw_rate)
            except ValueError as e:
                raise ConfigurationError(
                    f"SENTRY_TRACES_SAMPLE_RATE must be a float, got {raw_rate!r}"
                ) from e

    settings = Settings(
        env=env,
        git_sha=git_sha,
        app_url=app_url,
        pagehub_auth_base_url=pagehub_auth_base_url,
        pagehub_auth_issuer=pagehub_auth_issuer,
        service_api_key=service_api_key,
        jwt_signing_keys=signing_keys,
        app_slug=app_slug,
        runs_enabled=runs_enabled,
        database_url=database_url,
        encryption_key=encryption_key,
        admin_emails=admin_emails,
        sentry_dsn=sentry_dsn,
        sentry_traces_sample_rate=sentry_traces_sample_rate,
    )
    _settings_singleton = settings
    return settings


def override_settings(settings: Settings) -> None:
    """Test-only override. Reset with ``reset_settings()`` between tests."""
    global _settings_singleton
    _settings_singleton = settings


def reset_settings() -> None:
    global _settings_singleton
    _settings_singleton = None
