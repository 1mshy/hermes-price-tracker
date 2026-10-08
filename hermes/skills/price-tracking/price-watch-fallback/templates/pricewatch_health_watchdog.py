#!/usr/bin/env python3
"""Recover missed shopping work; notify once only when a problem persists.

A no_agent cron job, independent of the LLM. Diagnostics stay in
$HERMES_HOME/cron/health_watchdog_status.json. There is never stdout to deliver
as a second notification. A confirmed incident is pushed once, after two
observations, and can alert again only after it has actually recovered.
"""
import datetime as dt
import fcntl
import json
import os
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

BASE = os.environ.get("PRICEWATCH_BASE", "http://pricewatch:8000").rstrip("/")
HERMES_HOME = os.environ.get("HERMES_HOME", "/opt/data")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
NTFY_TOPIC = os.environ.get("NTFY_PUBLISH_TOPIC") or os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = (os.environ.get("NTFY_SERVER") or os.environ.get("NTFY_SERVER_URL")
               or "https://ntfy.sh").rstrip("/")
MAX_CONSECUTIVE_ERRORS = 3
FAILURE_STREAK = 3
PERSISTENT_RUNS = 2
SWEEP_GRACE_MINUTES = 15
NETWORK_PROBE_DELAYS = (0, 3, 5, 10, 20, 30)
RETRY_DELAYS = (2, 5, 15)
STATE_PATH = os.path.join(HERMES_HOME, "cron", "health_watchdog_state.json")
STATUS_PATH = os.path.join(HERMES_HOME, "cron", "health_watchdog_status.json")
TITLE = "Shopping watch needs attention"


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def as_utc(value):
    parsed = dt.datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
            return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=2)
    os.replace(temporary, path)


def transient(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (429, 500, 502, 503, 504)
    return isinstance(exc, (urllib.error.URLError, OSError, socket.timeout))


def wait_for_network(hosts):
    pending = {host for host in hosts if host}
    for delay in NETWORK_PROBE_DELAYS:
        if delay:
            time.sleep(delay)
        for host in list(pending):
            try:
                socket.getaddrinfo(host, 443)
                pending.remove(host)
            except OSError:
                pass
        if not pending:
            return True
    return False


def request_json(url, payload=None, timeout=20, headers=None, method=None):
    request_headers = dict(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            return json.loads(body) if body else {}
        except Exception as exc:
            if attempt < len(RETRY_DELAYS) and transient(exc):
                time.sleep(RETRY_DELAYS[attempt])
                continue
            raise


def get_json(url, timeout=20, headers=None):
    return request_json(url, timeout=timeout, headers=headers)


def post_json(url, payload, timeout=20):
    return request_json(url, payload, timeout=timeout, method="POST")


def overdue(sweep, state, now):
    """Respect the user's cadence, even daily/weekly watches.

    Remember the next run we actually observed rather than assuming that a
    listing older than three hours proves a broken scheduler.
    """
    expected = state.get("expected_sweep_at")
    if not expected or sweep.get("sweep_running"):
        return False
    try:
        due = as_utc(expected)
        last = sweep.get("last_sweep_at")
        return (now > due + dt.timedelta(minutes=SWEEP_GRACE_MINUTES)
                and (not last or as_utc(last) < due))
    except (TypeError, ValueError):
        return False


def recover_schedule(health, state, diagnostics):
    sweep = health.get("sweep") or {}
    if sweep.get("sweep_running"):
        return health
    disarmed = "next_sweep_at" in sweep and not sweep.get("next_sweep_at")
    stalled = overdue(sweep, state, utcnow())
    if not disarmed and not stalled:
        return health
    try:
        # Restore the existing cadence; never replace a user's schedule.
        if disarmed and sweep.get("cron"):
            request_json(f"{BASE}/api/schedule", {"cron": sweep["cron"]}, method="PATCH")
            diagnostics.append("Restored the existing sweep schedule.")
        post_json(f"{BASE}/api/refresh", {}, timeout=120)
        diagnostics.append("Requested a catch-up price check.")
        return get_json(f"{BASE}/api/health", timeout=15)
    except Exception as exc:
        diagnostics.append(f"Catch-up could not finish: {str(exc)[:160]}")
        return health


def coverage_problems(trackers, diagnostics):
    problems = {}
    for tracker in trackers:
        if not tracker.get("active"):
            continue
        currency = (tracker.get("currency") or "").upper()
        offers = [offer for offer in tracker.get("offers", [])
                  if not currency or (offer.get("currency") or "").upper() == currency]
        failed = [offer for offer in offers
                  if (offer.get("consecutive_errors") or 0) >= MAX_CONSECUTIVE_ERRORS]
        for offer in failed:
            diagnostics.append(f"{tracker.get('label') or tracker.get('title')}: "
                               f"{offer.get('store')}: {offer.get('error')}")
        # One blocked alternate store is not a broken watch. Newly queued
        # watches are retried by the engine and get no immediate error push.
        if not offers or len(failed) == len(offers):
            key = f"watch:{tracker['tracker_id']}"
            label = tracker.get("label") or tracker.get("title") or "Saved product"
            problems[key] = (f"{label}: no working listing in the watch's currency. "
                             "Automatic retries continue; add another retailer link to restore coverage.")
    return problems


def collect_problems(state):
    problems, diagnostics, observed = {}, [], {"engine:", "llm:", "cron:"}
    health, engine_up = {}, False
    try:
        # A broken notification hostname must never prevent price checks.
        wait_for_network([urlsplit(BASE).hostname])
        health = get_json(f"{BASE}/api/health", timeout=15)
        engine_up = True
        health = recover_schedule(health, state, diagnostics)
        if health.get("status") != "ok":
            problems["engine:health"] = "Price checks are unavailable. The pricewatch service needs attention."
        sweep = health.get("sweep") or {}
        if "next_sweep_at" in sweep and not sweep.get("next_sweep_at"):
            problems["engine:schedule"] = "Automatic price checks are not scheduled after a recovery attempt. Restart pricewatch."
        elif overdue(sweep, state, utcnow()):
            problems["engine:schedule"] = "Automatic price checks remain overdue after a catch-up attempt. Check the pricewatch service."
        try:
            trackers = get_json(f"{BASE}/api/trackers", timeout=30)["trackers"]
            problems.update(coverage_problems(trackers, diagnostics))
            observed.add("watch:")
        except Exception as exc:
            problems["engine:trackers"] = "Saved watches cannot be loaded. Check the pricewatch service."
            diagnostics.append(f"Loading watches: {str(exc)[:160]}")
    except Exception as exc:
        problems["engine:health"] = "Automatic price checks cannot reach pricewatch. Check that the shopping services are running."
        diagnostics.append(f"Price engine: {str(exc)[:160]}")

    jobs = read_json(os.path.join(HERMES_HOME, "cron", "jobs.json")).get("jobs", [])
    active = [job for job in jobs if job.get("enabled", True)
              and os.path.basename(job.get("script") or "") != os.path.basename(__file__)]
    model_down = False
    if LLM_BASE_URL and any(not job.get("no_agent") for job in active):
        headers = {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}
        try:
            get_json(f"{LLM_BASE_URL}/models", timeout=20, headers=headers)
        except Exception as exc:
            model_down = True
            problems["llm:endpoint"] = ("Community deal research cannot reach the model server. "
                                        "Start the configured model server; saved price watches continue independently.")
            diagnostics.append(f"Model endpoint: {str(exc)[:160]}")
    for job in active:
        if (job.get("failure_streak") or 0) < FAILURE_STREAK:
            continue
        if model_down and not job.get("no_agent"):
            continue  # The model outage explains these; send one incident.
        key = f"cron:{job.get('id') or job.get('name')}"
        problems[key] = (f"Scheduled research '{job.get('name')}' has repeatedly failed. "
                         "Check its saved run details; price watches continue independently.")
        diagnostics.append(f"{key}: {job.get('last_error') or job.get('last_status')}")
    return problems, diagnostics, observed, health, engine_up


def incident_state(problems, previous, observed, now):
    """Stable per-incident keys; recovery of one issue cannot resend another."""
    old = previous.get("incidents") or {}
    incidents = {key: value for key, value in old.items()
                 if not any(key.startswith(prefix) for prefix in observed)}
    due = {}
    for key, message in problems.items():
        incident = dict(old.get(key) or {})
        incident.setdefault("first_seen_at", now.isoformat())
        incident["runs"] = incident.get("runs", 0) + 1
        incident["message"] = message
        incidents[key] = incident
        if incident["runs"] >= PERSISTENT_RUNS and not incident.get("delivered_at"):
            due[key] = message
    return incidents, due


def push_report(report, engine_up, diagnostics):
    if engine_up:
        try:
            result = post_json(f"{BASE}/api/notify", {"title": TITLE, "message": report})
            if result.get("delivered"):
                return True
            diagnostics.append(f"No engine channel delivered: {result.get('channels') or result.get('error')}")
        except Exception as exc:
            diagnostics.append(f"Engine notification: {str(exc)[:160]}")
    if NTFY_TOPIC:
        try:
            # Only resolve ntfy when a message is actually due.
            wait_for_network([urlsplit(NTFY_SERVER).hostname])
            post_json(NTFY_SERVER, {"topic": NTFY_TOPIC, "title": TITLE,
                                   "message": report, "priority": 3, "tags": ["warning"]})
            return True
        except Exception as exc:
            diagnostics.append(f"Notification retry: {str(exc)[:160]}")
    return False


def run():
    state = read_json(STATE_PATH)
    now = utcnow()
    problems, diagnostics, observed, health, engine_up = collect_problems(state)
    incidents, due = incident_state(problems, state, observed, now)
    sweep = health.get("sweep") or {}
    next_state = {"incidents": incidents, "checked_at": now.isoformat(),
                  "expected_sweep_at": (state.get("expected_sweep_at") if overdue(sweep, state, now)
                                        else sweep.get("next_sweep_at") or state.get("expected_sweep_at"))}
    # Prove state is writable before sending, so a permissions problem cannot
    # turn into the same notification on every run.
    save_json(STATE_PATH, next_state)
    if due and push_report("\n".join(f"- {message}" for message in due.values()), engine_up, diagnostics):
        for key in due:
            incidents[key]["delivered_at"] = now.isoformat()
        save_json(STATE_PATH, next_state)
    save_json(STATUS_PATH, {"checked_at": now.isoformat(), "problems": problems,
                           "actions_and_details": diagnostics})
    return 0


def main():
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH + ".lock", "w", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 0
            return run()
    except Exception as exc:
        # A diagnostic failure must not become a cron failure push itself.
        try:
            save_json(STATUS_PATH, {"checked_at": utcnow().isoformat(),
                                   "error": str(exc)[:300]})
        except OSError:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
