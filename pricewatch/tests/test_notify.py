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


# ── dispatch: retry the transport, never the answer ──────────────────────
import httpx

from pricewatch import notify
from pricewatch.notify.base import Channel


class _FlakyChannel(Channel):
    name = "flaky"

    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0

    @property
    def configured(self):
        return True

    async def send(self, alert):
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)


def _dispatch_through(monkeypatch, channel):
    monkeypatch.setattr(notify, "CHANNELS", [channel])
    monkeypatch.setattr(notify, "RETRY_DELAYS", (0, 0, 0))
    return asyncio.run(notify.dispatch(Alert(title="T", body="B")))


def test_dispatch_retries_dns_and_connect_failures(monkeypatch):
    dns_down = httpx.ConnectError("[Errno -5] No address associated with hostname")
    channel = _FlakyChannel([dns_down, OSError("network unreachable")])
    assert _dispatch_through(monkeypatch, channel) == {"flaky": "sent"}
    assert channel.calls == 3


def test_dispatch_does_not_retry_an_http_status(monkeypatch):
    channel = _FlakyChannel([RuntimeError("ntfy 429: too many requests")])
    out = _dispatch_through(monkeypatch, channel)
    assert out == {"flaky": "error: ntfy 429: too many requests"}
    assert channel.calls == 1


def test_dispatch_gives_up_after_the_retry_budget(monkeypatch):
    channel = _FlakyChannel([httpx.ConnectError("down")] * 10)
    out = _dispatch_through(monkeypatch, channel)
    assert out["flaky"].startswith("error: down")
    assert channel.calls == len((0, 0, 0)) + 1


def test_transient_classifies_transport_errors_only():
    assert notify.transient(httpx.ConnectError("x"))
    assert notify.transient(httpx.ReadTimeout("x"))
    assert notify.transient(OSError("x"))
    assert not notify.transient(RuntimeError("discord webhook 400"))
    assert not notify.transient(ValueError("bad payload"))


# Provider responses are distinct from an untyped application/payload error.
import pytest
from pricewatch.notify.base import DeliveryError


@pytest.mark.parametrize('status', [408, 425, 429, 500, 502, 503, 504])
def test_dispatch_retries_temporary_provider_failures(monkeypatch, status):
    channel = _FlakyChannel([DeliveryError('ntfy', status, 'temporary')])
    assert _dispatch_through(monkeypatch, channel) == {'flaky': 'sent'}
    assert channel.calls == 2


@pytest.mark.parametrize('status', [400, 401, 403, 404, 413])
def test_dispatch_does_not_retry_permanent_provider_failures(monkeypatch, status):
    channel = _FlakyChannel([DeliveryError('ntfy', status, 'invalid')])
    assert _dispatch_through(monkeypatch, channel)['flaky'].startswith('error:')
    assert channel.calls == 1


def test_retry_after_is_respected_without_blocking_other_channels(monkeypatch):
    waits = []
    good = _FlakyChannel([])
    good.name = 'good'
    slow = _FlakyChannel([DeliveryError('ntfy', 429, 'rate limit', '7')])
    async def sleep(delay):
        waits.append(delay)
    monkeypatch.setattr(notify.asyncio, 'sleep', sleep)
    monkeypatch.setattr(notify, 'CHANNELS', [good, slow])
    assert asyncio.run(notify.dispatch(Alert('T', 'B'))) == {'good': 'sent', 'flaky': 'sent'}
    assert waits == [7]
    assert good.calls == 1


def test_long_retry_after_defers_until_a_later_sweep(monkeypatch):
    monkeypatch.setattr(notify, '_deferred_until', {})
    monkeypatch.setattr(notify, 'monotonic', lambda: 100)
    channel = _FlakyChannel([DeliveryError('ntfy', 429, 'rate limit', '3600')])
    assert 'deferred' in _dispatch_through(monkeypatch, channel)['flaky']
    # A manual sweep before the provider's deadline must not send early.
    assert 'deferred' in _dispatch_through(monkeypatch, channel)['flaky']
    assert channel.calls == 1
    monkeypatch.setattr(notify, 'monotonic', lambda: 3701)
    assert _dispatch_through(monkeypatch, channel) == {'flaky': 'sent'}
    assert channel.calls == 2


def test_retry_after_http_date_and_invalid_value():
    from email.utils import format_datetime
    import datetime as dt
    date = format_datetime(dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=20))
    assert 18 <= DeliveryError('ntfy', 503, '', date).retry_after <= 20
    assert DeliveryError('ntfy', 503, '', 'invalid').retry_after == 0


def test_real_ntfy_response_retries_then_succeeds(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) == 1 else 200, json={'ok': True})
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            _patch_client(monkeypatch, client)
            return await notify.dispatch(Alert('Recovery test', 'One message'))
    monkeypatch.setattr(settings, 'ntfy_topic', 'local-test')
    monkeypatch.setattr(notify, 'CHANNELS', [NtfyChannel()])
    monkeypatch.setattr(notify, 'RETRY_DELAYS', (0,))
    assert asyncio.run(exercise()) == {'ntfy': 'sent'}
    assert len(calls) == 2
