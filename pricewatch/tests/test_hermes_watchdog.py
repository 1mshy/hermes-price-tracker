"""Shopping cron recovery/delivery with all network and notifications substituted."""
import datetime as dt
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


watchdog = load('health_watchdog', ROOT / 'hermes/skills/price-tracking/price-watch-fallback/templates/pricewatch_health_watchdog.py')
reconcile = load('shopping_jobs', ROOT / 'hermes/scripts/reconcile_shopping_jobs.py')
NOW = dt.datetime(2026, 9, 30, 12, tzinfo=dt.timezone.utc)


@pytest.fixture
def watch(monkeypatch, tmp_path):
    monkeypatch.setattr(watchdog, 'HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(watchdog, 'STATE_PATH', str(tmp_path / 'cron/state.json'))
    monkeypatch.setattr(watchdog, 'STATUS_PATH', str(tmp_path / 'cron/status.json'))
    monkeypatch.setattr(watchdog, 'utcnow', lambda: NOW)
    monkeypatch.setattr(watchdog, 'wait_for_network', lambda hosts: True)
    monkeypatch.setattr(watchdog, 'LLM_BASE_URL', '')
    monkeypatch.setattr(watchdog, 'NTFY_TOPIC', '')
    monkeypatch.setattr(watchdog, 'request_json', lambda *a, **kw: pytest.fail('unexpected network'))
    return watchdog


def test_persistent_incident_pushes_once_and_stays_quiet_as_details_change(watch, monkeypatch, capsys):
    issue = {'llm:endpoint': 'Model unavailable'}
    monkeypatch.setattr(watch, 'collect_problems', lambda state: (issue, [], {'llm:'}, {}, True))
    pushes = []
    monkeypatch.setattr(watch, 'push_report', lambda report, *a: pushes.append(report) or True)
    assert watch.main() == 0
    assert pushes == []  # transient first observation
    assert watch.main() == 0
    assert len(pushes) == 1
    issue['llm:endpoint'] = 'Model still unavailable, different failure details'
    monkeypatch.setattr(watch, 'utcnow', lambda: NOW + dt.timedelta(days=8))
    assert watch.main() == 0
    assert len(pushes) == 1  # no daily or weekly reminder spam
    assert capsys.readouterr().out == ''  # no duplicate cron final response


def test_clearing_one_issue_does_not_resend_another(watch):
    old = {'incidents': {
        'watch:1': {'runs': 4, 'delivered_at': NOW.isoformat()},
        'llm:endpoint': {'runs': 4, 'delivered_at': NOW.isoformat()},
    }}
    state, due = watch.incident_state({'llm:endpoint': 'Still down'}, old, {'watch:', 'llm:'}, NOW)
    assert list(state) == ['llm:endpoint']
    assert due == {}


def test_recovered_incident_can_alert_again_only_after_new_persistence(watch):
    prior = {'incidents': {'watch:1': {'runs': 3, 'delivered_at': NOW.isoformat()}}}
    state, _ = watch.incident_state({}, prior, {'watch:'}, NOW)
    assert state == {}
    state, due = watch.incident_state({'watch:1': 'No coverage'}, {'incidents': state}, {'watch:'}, NOW)
    assert due == {}
    _, due = watch.incident_state({'watch:1': 'No coverage'}, {'incidents': state}, {'watch:'}, NOW)
    assert list(due) == ['watch:1']


def test_unobserved_watches_are_not_falsely_marked_recovered(watch):
    prior = {'incidents': {'watch:1': {'runs': 3, 'delivered_at': NOW.isoformat()}}}
    state, due = watch.incident_state({'engine:health': 'Down'}, prior, {'engine:'}, NOW)
    assert state['watch:1']['delivered_at'] == NOW.isoformat()
    assert due == {}


def test_failed_delivery_is_retried_without_becoming_delivered(watch, monkeypatch):
    monkeypatch.setattr(watch, 'collect_problems', lambda state: ({'watch:1': 'No coverage'}, [], {'watch:'}, {}, True))
    attempts = []
    monkeypatch.setattr(watch, 'push_report', lambda *a: attempts.append(1) and False)
    watch.main()
    watch.main()
    watch.main()
    assert len(attempts) == 2
    assert 'delivered_at' not in watch.read_json(watch.STATE_PATH)['incidents']['watch:1']
    monkeypatch.setattr(watch, 'push_report', lambda *a: True)
    watch.main()
    assert watch.read_json(watch.STATE_PATH)['incidents']['watch:1']['delivered_at']


def test_one_failed_alternate_does_not_mean_watch_has_no_coverage(watch):
    offers = [{'currency': 'CAD', 'consecutive_errors': 20, 'error': 'blocked'},
              {'currency': 'CAD', 'consecutive_errors': 0, 'price': 99}]
    tracker = {'tracker_id': 1, 'active': True, 'currency': 'CAD', 'offers': offers}
    assert watch.coverage_problems([tracker], []) == {}
    offers[1]['currency'] = 'USD'
    assert list(watch.coverage_problems([tracker], [])) == ['watch:1']
    tracker['active'] = False
    assert watch.coverage_problems([tracker], []) == {}


def test_schedule_recovery_respects_daily_cadence(watch):
    last = (NOW - dt.timedelta(hours=23)).isoformat()
    due = (NOW + dt.timedelta(hours=1)).isoformat()
    health = {'sweep': {'cron': '0 13 * * *', 'last_sweep_at': last, 'next_sweep_at': due}}
    assert watch.recover_schedule(health, {'expected_sweep_at': due}, []) == health


def test_overdue_sweep_gets_one_catchup_and_rechecks_health(watch, monkeypatch):
    previous_due = (NOW - dt.timedelta(hours=1)).isoformat()
    health = {'sweep': {'next_sweep_at': (NOW + dt.timedelta(hours=1)).isoformat(),
                        'last_sweep_at': (NOW - dt.timedelta(hours=2)).isoformat()}}
    recovered = {'sweep': {**health['sweep'], 'last_sweep_at': NOW.isoformat()}}
    calls = []
    monkeypatch.setattr(watch, 'post_json', lambda url, *a, **kw: calls.append(url) or {})
    monkeypatch.setattr(watch, 'get_json', lambda *a, **kw: recovered)
    assert watch.recover_schedule(health, {'expected_sweep_at': previous_due}, []) == recovered
    assert calls == [watch.BASE + '/api/refresh']
    assert not watch.overdue(recovered['sweep'], {'expected_sweep_at': previous_due}, NOW)


def test_disarmed_schedule_is_restored_without_changing_cron(watch, monkeypatch):
    health = {'sweep': {'cron': '0 13 * * *', 'next_sweep_at': None}}
    calls = []
    monkeypatch.setattr(watch, 'request_json', lambda url, payload=None, **kw: calls.append((url, payload, kw)) or {})
    watch.recover_schedule(health, {}, [])
    assert calls[0] == (watch.BASE + '/api/schedule', {'cron': '0 13 * * *'}, {'method': 'PATCH'})
    assert calls[1][0] == watch.BASE + '/api/refresh'


def test_notification_dns_cannot_block_health_checks(watch, monkeypatch):
    hosts = []
    monkeypatch.setattr(watch, 'NTFY_TOPIC', 'alerts')
    monkeypatch.setattr(watch, 'wait_for_network', lambda names: hosts.extend(names) or True)
    health = {'status': 'ok', 'sweep': {'next_sweep_at': (NOW + dt.timedelta(hours=1)).isoformat()}}
    monkeypatch.setattr(watch, 'get_json', lambda url, **kw: health if url.endswith('/health') else {'trackers': []})
    problems, _, _, _, up = watch.collect_problems({})
    assert up and not problems
    assert hosts == ['pricewatch']


def test_notify_api_http_success_without_delivery_falls_back(watch, monkeypatch):
    monkeypatch.setattr(watch, 'NTFY_TOPIC', 'alerts')
    calls = []
    monkeypatch.setattr(watch, 'post_json', lambda url, payload: calls.append(url) or {'delivered': False})
    assert watch.push_report('An actionable incident', True, [])
    assert calls == [watch.BASE + '/api/notify', watch.NTFY_SERVER]


def test_cron_upgrade_preserves_custom_text_schedule_and_unrelated_jobs(monkeypatch):
    monkeypatch.setattr(reconcile, 'PROMPTS', ROOT / 'hermes/cron-prompts')
    jobs = [{'id': 'morning', 'name': 'morning-deals-brief', 'deliver': 'discord',
             'prompt': 'Only search my custom electronics list.', 'enabled': False,
             'schedule': {'expr': '0 15 * * *'}},
            {'id': 'other', 'name': 'Personal reminder', 'deliver': 'discord', 'prompt': 'Do not touch'}]
    updates = []
    state = reconcile.reconcile(jobs, lambda job_id, values: updates.append((job_id, values)), {})
    assert len(updates) == 1
    job_id, values = updates[0]
    assert job_id == 'morning'
    assert values['deliver'] == 'local'
    assert values['prompt'].startswith(jobs[0]['prompt'])
    assert set(values) == {'deliver', 'prompt'}
    jobs[0].update(values)
    assert reconcile.reconcile(jobs, lambda *a: pytest.fail('not idempotent'), state) == state


def test_managed_cron_templates_update_and_user_edits_are_preserved(monkeypatch):
    monkeypatch.setattr(reconcile, 'PROMPTS', ROOT / 'hermes/cron-prompts')
    job = {'id': 'morning', 'name': 'morning-deals-brief', 'deliver': 'local', 'prompt': 'Old managed text'}
    state = {'morning': {'managed': True, 'hash': reconcile.digest(job['prompt'])}}
    updates = []
    state = reconcile.reconcile([job], lambda _, values: updates.append(values), state)
    assert 'active pricewatch trackers' in updates[0]['prompt']
    job.update(updates[0])
    job['prompt'] += '\nOnly check on weekdays.'
    updates.clear()
    state = reconcile.reconcile([job], lambda _, values: updates.append(values), state)
    assert 'Only check on weekdays.' in updates[0]['prompt']
    assert not state['morning']['managed']


def test_cron_policy_preserves_user_text_appended_after_managed_block(monkeypatch):
    monkeypatch.setattr(reconcile, 'PROMPTS', ROOT / 'hermes/cron-prompts')
    job = {'id': 'morning', 'name': 'morning-deals-brief', 'deliver': 'local',
           'prompt': 'My custom products' + reconcile.POLICY_MARKER + reconcile.POLICY.strip()
                     + reconcile.POLICY_END + '\nOnly check on weekends.'}
    updates = []
    reconcile.reconcile([job], lambda _, values: updates.append(values), {})
    prompt = updates[0]['prompt']
    assert 'My custom products' in prompt
    assert 'Only check on weekends.' in prompt
    assert prompt.count(reconcile.POLICY_MARKER) == 1
