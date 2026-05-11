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
    "ENCRYPTION_KEY": "ZmVybmV0LXRlc3Qta2V5LTMyLWJ5dGVzLWJhc2U2NA==",
    "ADMIN_EMAILS": "support@pagehub.io",
}
for _k, _v in _TEST_ENV.items():
    os.environ.setdefault(_k, _v)


@pytest.fixture(autouse=True)
def _scaffold_env(monkeypatch):
    for k, v in _TEST_ENV.items():
        monkeypatch.setenv(k, v)
    reset_settings()
    yield
    reset_settings()
