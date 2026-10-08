import argparse
import asyncio

import pytest

from pricewatch import verify


@pytest.mark.parametrize('status', ['TIMEOUT', 'FAIL', 'ERROR', 'NO-URL', 'UNREACHABLE'])
def test_strict_verification_fails_on_unverified_store(monkeypatch, status):
    async def run(*args):
        assert verify.settings.pw_browser_enabled is False
        return [{'status': status}]
    async def close():
        pass
    monkeypatch.setattr(verify, 'run', run)
    monkeypatch.setattr(verify, 'render_table', lambda rows: '')
    monkeypatch.setattr(verify.fetcher, 'aclose', close)
    monkeypatch.setattr(verify.settings, 'pw_browser_enabled', True)
    args = argparse.Namespace(tag=None, store=None, no_browser=True, concurrency=1,
                              timeout=1, json=None, strict=True)
    assert asyncio.run(verify.main_async(args)) == 1
    assert verify.settings.pw_browser_enabled is True
