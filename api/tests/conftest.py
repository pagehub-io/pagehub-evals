"""Pytest fixtures for the scaffold.

Sets every env var api.config.get_settings() requires. There are no
defaults — see api/config.py — so the test suite has to be explicit
too. Real test fixtures (db pool, client) land alongside the JTBD.
"""

import os

import pytest

from api.config import reset_settings

# Module-level so the envs are present BEFORE pytest collects test
# modules (api.main runs get_settings() at import time). Per-test
# monkeypatch below still overrides for isolation.
_TEST_ENV = {
    "ENVIRONMENT": "test",
    "GIT_SHA": "test",
    "APP_URL": "http://localhost:8002",
    "PAGEHUB_AUTH_BASE_URL": "http://localhost:8080",
    "PAGEHUB_AUTH_ISSUER": "http://localhost:8080",
    "SERVICE_API_KEY": "test-service-api-key",
    "JWT_SIGNING_KEYS": "test-kid:test-secret",
    "APP_SLUG": "pagehub-evals",
    "RUNS_ENABLED": "true",
    "DATABASE_URL": "postgresql://test:test@localhost/test",
    # A *valid* 32-byte url-safe-base64 Fernet key (the previous test value
    # decoded to 31 bytes and Fernet rejected it — only mattered once a test
    # actually called encrypt()). Same key as docker-compose's dev value.
    "ENCRYPTION_KEY": "6bzQXvxLe_oere1FNN-mWtRwyQXFUJBaOw_R7iYvcX8=",
    "ADMIN_EMAILS": "support@pagehub.io",
}
for _k, _v in _TEST_ENV.items():
    os.environ.setdefault(_k, _v)

# DATABASE_URL is special: DB-backed tests (api/tests/_db.py) connect to it
# directly, and CI provides a real one. The per-test monkeypatch below must
# NOT clobber an externally-set value — only fall back to the bogus default
# when nothing real is configured (the no-DB unit run).
_PRESERVE_FROM_ENV = {"DATABASE_URL"}


@pytest.fixture(autouse=True)
def _scaffold_env(monkeypatch):
    for k, v in _TEST_ENV.items():
        if k in _PRESERVE_FROM_ENV:
            monkeypatch.setenv(k, os.environ.get(k, v))
        else:
            monkeypatch.setenv(k, v)
    reset_settings()
    yield
    reset_settings()
