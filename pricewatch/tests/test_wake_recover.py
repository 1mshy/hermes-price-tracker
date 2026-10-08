"""Host recovery decisions, with Docker/power/network operations substituted."""
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / 'scripts/host/wake_recover.py'
spec = importlib.util.spec_from_file_location('wake_recover', MODULE_PATH)
wake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wake)


@pytest.fixture
def host(monkeypatch, tmp_path):
    monkeypatch.setattr(wake, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(wake, 'STATE_PATH', tmp_path / 'state.json')
    monkeypatch.setattr(wake, 'LOCK_PATH', tmp_path / 'lock')
    monkeypatch.setattr(wake, 'log', lambda msg: None)
    monkeypatch.setattr(wake, 'hold_awake', lambda: None)
    monkeypatch.setattr(wake, 'read_env', lambda: {})
    monkeypatch.setattr(wake, 'docker_ready', lambda **kw: True)
    monkeypatch.setattr(wake, 'services', lambda: ['pricewatch', 'hermes'])
    monkeypatch.setattr(wake, 'all_healthy', lambda wanted: True)
    monkeypatch.setattr(wake, 'ensure_containers', lambda *args: True)
    monkeypatch.setattr(wake, 'on_ac_power', lambda: True)
    monkeypatch.setattr(wake, 'last_wake', lambda: None)
    monkeypatch.setattr(wake, 'wait_for_engine', lambda *args, **kw: {'status': 'ok'})
    monkeypatch.setattr(wake, 'wait_for_dns', lambda *args, **kw: True)
    monkeypatch.setattr(wake, 'refresh_engine', lambda *args: True)
    monkeypatch.setattr(wake, 'rerun_failed_jobs', lambda *args, **kw: 0)
    monkeypatch.setattr(sys, 'argv', ['wake_recover.py', '--settle', '0'])
    return wake


def test_dns_blackout_is_retried_next_interval_even_with_healthy_containers(host, monkeypatch):
    calls = []
    monkeypatch.setattr(host, 'wait_for_dns', lambda *args, **kw: False)
    monkeypatch.setattr(host, 'refresh_engine', lambda *args: calls.append(1) or True)
    assert host.main() == 1
    assert host.load_state()['catch_up_pending'] is True
    assert calls == []
    monkeypatch.setattr(host, 'wait_for_dns', lambda *args, **kw: True)
    assert host.main() == 0
    assert calls == [1]
    assert host.load_state()['catch_up_pending'] is False
    assert host.main() == 0
    assert calls == [1]  # quiet steady state, no extra retailer traffic


def test_failed_refresh_does_not_consume_catchup(host, monkeypatch):
    monkeypatch.setattr(host, 'refresh_engine', lambda *args: False)
    assert host.main() == 1
    assert host.load_state()['catch_up_pending'] is True


def test_short_sleep_detected_by_kernel_wake_time(host, monkeypatch):
    host.save_state({'last_run': time.time(), 'last_wake': 100, 'catch_up_pending': False})
    monkeypatch.setattr(host, 'last_wake', lambda: 200)
    calls = []
    monkeypatch.setattr(host, 'refresh_engine', lambda *args: calls.append(1) or True)
    assert host.main() == 0
    assert calls == [1]
    assert host.load_state()['last_wake'] == 200
    assert host.main() == 0
    assert calls == [1]


def test_stopped_stack_catches_up_even_without_a_sleep_gap(host, monkeypatch):
    host.save_state({'last_run': time.time(), 'catch_up_pending': False})
    monkeypatch.setattr(host, 'all_healthy', lambda wanted: False)
    calls = []
    monkeypatch.setattr(host, 'refresh_engine', lambda *args: calls.append(1) or True)
    assert host.main() == 0
    assert calls == [1]


def test_failed_compose_recovery_is_not_reported_as_success(host, monkeypatch):
    monkeypatch.setattr(host, 'ensure_containers', lambda *args: False)
    assert host.main() == 1
    assert host.load_state()['catch_up_pending'] is True


def test_docker_is_left_off_on_battery_but_catchup_stays_pending(host, monkeypatch):
    monkeypatch.setattr(host, 'docker_ready', lambda **kw: False)
    monkeypatch.setattr(host, 'on_ac_power', lambda: False)
    monkeypatch.delenv('HS_START_DOCKER_ON_BATTERY', raising=False)
    monkeypatch.setattr(host, 'start_docker', lambda **kw: pytest.fail('must not start'))
    assert host.main() == 0
    assert host.load_state()['catch_up_pending'] is True


def test_unhealthy_containers_are_restarted_before_compose_up(host, monkeypatch):
    calls = []
    monkeypatch.undo()
    monkeypatch.setattr(wake, 'container_health', lambda: {
        'pricewatch': ('running', 'unhealthy'), 'hermes': ('running', 'healthy')})
    monkeypatch.setattr(wake, 'log', lambda msg: None)
    def compose(*args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, '', '')
    monkeypatch.setattr(wake, 'compose', compose)
    assert wake.ensure_containers(['pricewatch', 'hermes'], False)
    assert calls[0] == ('restart', 'pricewatch')
    assert calls[1][0] == 'up'


@pytest.mark.parametrize('data', ['[]', '{broken', '{"last_run":"broken"}'])
def test_corrupt_state_recovers_as_first_run(host, data):
    host.STATE_PATH.write_text(data)
    assert host.main() == 0
    assert host.load_state()['catch_up_pending'] is False


def test_installer_uses_calendar_events_that_coalesce_on_wake():
    import plistlib
    installer = MODULE_PATH.with_name('install-wake-agent.sh')
    result = subprocess.run(['bash', str(installer), '--print-plist'],
                            capture_output=True, check=True)
    config = plistlib.loads(result.stdout)
    assert 'StartInterval' not in config
    assert config['StartCalendarInterval'] == [{'Minute': minute} for minute in range(0, 60, 2)]
    assert config['RunAtLoad'] is True


@pytest.mark.parametrize('interval', ['0', '-1', '30', 'abc', '90', '420'])
def test_installer_rejects_invalid_calendar_interval(interval):
    result = subprocess.run(['bash', str(MODULE_PATH.with_name('install-wake-agent.sh')),
                             '--print-plist', '--interval', interval], capture_output=True)
    assert result.returncode != 0
