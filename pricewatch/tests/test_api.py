"""API smoke tests that need no network: schedule + alerts endpoints."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pricewatch.api import router
from pricewatch.db import init_db


def _client() -> TestClient:
    init_db()
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


def test_health():
    response = _client().get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_schedule_roundtrip():
    client = _client()
    base = client.get("/api/schedule").json()
    assert "cron" in base and "next_sweep_at" in base

    ok = client.patch("/api/schedule", json={"cron": "0 */3 * * *"})
    assert ok.status_code == 200
    assert ok.json()["cron"] == "0 */3 * * *"

    reverted = client.patch("/api/schedule", json={"cron": "default"})
    assert reverted.status_code == 200
    assert reverted.json()["source"] == "env"


def test_schedule_rejects_impolite_cron():
    response = _client().patch("/api/schedule", json={"cron": "* * * * *"})
    assert response.status_code == 422
    assert "floor" in response.json()["detail"]


def test_alerts_listing():
    response = _client().get("/api/alerts")
    assert response.status_code == 200
    assert "alerts" in response.json()


def test_stores_listing():
    response = _client().get("/api/stores")
    assert response.status_code == 200
    stores = response.json()["stores"]
    assert any(s["key"] == "bambulab" for s in stores)


def test_notify_endpoint_pushes_through_the_alert_channels(monkeypatch):
    from pricewatch import service
    sent = []

    async def dispatch(alert, only=None):
        sent.append(alert)
        return {"ntfy": "sent"}
    monkeypatch.setattr(service, "dispatch", dispatch)
    response = _client().post("/api/notify", json={
        "title": "watchdog", "message": "LLM endpoint unreachable", "url": ""})
    assert response.status_code == 200
    assert response.json()["ok"] is True and response.json()["channels"] == {"ntfy": "sent"}
    assert sent[-1].body == "LLM endpoint unreachable"
