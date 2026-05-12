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


def test_metrics_exposes_runs_engine_metrics():
    # PLAN.md:156-160 names three metrics; import the engine module to
    # force registration, then verify the names appear in /metrics.
    import api.runs._metrics  # noqa: F401 — side effect: register metrics

    with TestClient(app) as client:
        r = client.get("/metrics")
    body = r.text
    assert "pagehub_evals_runs_total" in body
    assert "pagehub_evals_run_duration_seconds" in body
    assert "pagehub_evals_run_request_count" in body
