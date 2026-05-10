"""Pytest fixtures for the scaffold.

Sets every env var api.config.get_settings() requires. There are no
defaults — see api/config.py — so the test suite has to be explicit
too. Real test fixtures (db pool, client) land alongside the JTBD.
"""

import pytest

from api.config import reset_settings


@pytest.fixture(autouse=True)
def _scaffold_env(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("GIT_SHA", "test")
    monkeypatch.setenv("APP_URL", "http://localhost:8002")
    monkeypatch.setenv("PAGEHUB_AUTH_BASE_URL", "http://localhost:8080")
    monkeypatch.setenv("PAGEHUB_AUTH_ISSUER", "http://localhost:8080")
    monkeypatch.setenv("SERVICE_API_KEY", "test-service-api-key")
    monkeypatch.setenv("JWT_SIGNING_KEYS", "test-kid:test-secret")
    monkeypatch.setenv("APP_SLUG", "pagehub-evals")
    monkeypatch.setenv("RUNS_ENABLED", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost/test")
    monkeypatch.setenv("ENCRYPTION_KEY", "ZmVybmV0LXRlc3Qta2V5LTMyLWJ5dGVzLWJhc2U2NA==")
    monkeypatch.setenv("ADMIN_EMAILS", "support@pagehub.io")
    reset_settings()
    yield
    reset_settings()
