import asyncio

from pricewatch import fetch
from pricewatch.notify import channel_status
from pricewatch.notify.base import Alert
from pricewatch.notify.ntfy import NtfyChannel
from pricewatch.settings import settings


class _FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class _FakeClient:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.calls = []

    async def post(self, url, json=None):
        self.calls.append((url, json))
        return _FakeResponse(self.status_code)


def _patch_client(monkeypatch, client):
    async def fake_client():
        return client
    monkeypatch.setattr(fetch.fetcher, "client", fake_client)


def test_ntfy_unconfigured_by_default():
    status = channel_status()
    assert "ntfy" in status
    assert status["ntfy"] is False


def test_ntfy_sends_json_publish(monkeypatch):
    monkeypatch.setattr(settings, "ntfy_topic", "hermes-test-topic")
    client = _FakeClient()
    _patch_client(monkeypatch, client)

    channel = NtfyChannel()
    assert channel.configured
    asyncio.run(channel.send(Alert(
        title="💸 Deal", body="now cheap", url="https://store/x",
        price=42.0, currency="USD", store="west3d")))

    url, payload = client.calls[0]
    assert url == "https://ntfy.sh"
    assert payload["topic"] == "hermes-test-topic"
    assert payload["title"].startswith("💸")
    assert payload["click"] == "https://store/x"
    assert payload["priority"] == 4


def test_ntfy_custom_server(monkeypatch):
    monkeypatch.setattr(settings, "ntfy_topic", "t")
    monkeypatch.setattr(settings, "ntfy_server", "https://ntfy.example.com/")
    client = _FakeClient()
    _patch_client(monkeypatch, client)
    asyncio.run(NtfyChannel().send(Alert(title="T", body="B")))
    assert client.calls[0][0] == "https://ntfy.example.com"


def test_ntfy_http_error_raises(monkeypatch):
    monkeypatch.setattr(settings, "ntfy_topic", "t")
    _patch_client(monkeypatch, _FakeClient(status_code=429))
    try:
        asyncio.run(NtfyChannel().send(Alert(title="T", body="B")))
    except RuntimeError as exc:
        assert "429" in str(exc)
    else:
        raise AssertionError("expected RuntimeError on non-2xx")
