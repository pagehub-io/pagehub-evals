"""Health-endpoint smoke test — proves the FastAPI app boots."""

from fastapi.testclient import TestClient

from api.main import app


def test_root_returns_app_info():
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "pagehub-evals"
    assert body["docs"] == "/docs"


def test_health_returns_ok():
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["env"] == "test"


def test_metrics_returns_prometheus_payload():
    with TestClient(app) as client:
        r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers.get("content-type", "")
