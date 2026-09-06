#!/usr/bin/env python3
"""Silent health watchdog (no_agent cron job) that pushes its own report.

Prints ONLY when something is wrong; empty stdout = healthy = no message (the
watchdog pattern). Checks, in the order they fail most quietly:

1. the price engine answers /api/health;
2. sweeps are actually happening (the newest `last_checked` is recent);
3. no tracked listing has failed MAX_CONSECUTIVE_ERRORS sweeps in a row;
4. the LLM endpoint the agent runs on answers /models — when it is down every
   agent cron job (Reddit sweeps, the morning brief) fails and nothing else
   says so: the community sweep failed eight runs in a row over 2026-09-03/05
   while a $49.99 coupon on a watched product came and went;
5. no cron job has a failure streak of FAILURE_STREAK or more.

A no_agent job's stdout goes only where its `deliver` target points, and
`local` is a file inside the container. So on trouble this script also pushes
the report itself: through the engine's POST /api/notify (the same channels
the price alerts use) when the engine is up, straight to ntfy when the engine
is what is down. An unchanged report is not re-pushed for REPEAT_HOURS.

Install: copy to $HERMES_HOME/scripts/, then
    cronjob action=create no_agent=true script=pricewatch_health_watchdog.py schedule='0 */6 * * *'
"""
import datetime as dt
import json
import os
import sys
import urllib.request

BASE = os.environ.get("PRICEWATCH_BASE", "http://pricewatch:8000")
HERMES_HOME = os.environ.get("HERMES_HOME", "/opt/data")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
# In the agent container NTFY_TOPIC may be the agent's *inbound* topic; the
# alerts topic the user's phone subscribes to is NTFY_PUBLISH_TOPIC.
NTFY_TOPIC = os.environ.get("NTFY_PUBLISH_TOPIC") or os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = (os.environ.get("NTFY_SERVER") or os.environ.get("NTFY_SERVER_URL")
               or "https://ntfy.sh").rstrip("/")

MAX_CONSECUTIVE_ERRORS = 3
FAILURE_STREAK = 2
STALE_SWEEP_HOURS = 3
REPEAT_HOURS = 24
STATE_PATH = os.path.join(HERMES_HOME, "cron", "health_watchdog_state.json")
TITLE = "Hermes Shopping watchdog"


def get_json(url, timeout=20, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def post_json(url, payload, timeout=20):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def as_utc(value):
    parsed = dt.datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


problems = []
engine_up = True

# 1. engine
try:
    health = get_json(f"{BASE}/api/health", timeout=15)
    if health.get("status") != "ok":
        problems.append(f"engine health check returned {health}")
except Exception as exc:                               # noqa: BLE001
    engine_up = False
    problems.append(f"price engine unreachable ({exc}) — tracked prices are NOT being checked")

# 2 + 3. sweeps and listings
if engine_up:
    try:
        trackers = get_json(f"{BASE}/api/trackers", timeout=30)["trackers"]
    except Exception as exc:                           # noqa: BLE001
        trackers = []
        problems.append(f"could not list trackers ({exc})")
    newest = None
    for tracker in trackers:
        if not tracker.get("active"):
            continue
        for offer in tracker.get("offers", []):
            checked = offer.get("last_checked")
            if checked:
                when = as_utc(checked)
                newest = when if newest is None or when > newest else newest
            errors = offer.get("consecutive_errors") or 0
            if errors >= MAX_CONSECUTIVE_ERRORS:
                problems.append(
                    f"'{tracker.get('label', '?')}' at {offer.get('store')} has failed "
                    f"{errors} sweeps in a row ({str(offer.get('error'))[:80]}) — "
                    f"{offer.get('url')}")
    if newest is not None and utcnow() - newest > dt.timedelta(hours=STALE_SWEEP_HOURS):
        problems.append(f"no listing has been checked since {newest:%Y-%m-%d %H:%M} UTC — "
                        f"the sweep scheduler looks stalled")

# 4. the model behind the agent
if LLM_BASE_URL:
    headers = {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}
    try:
        get_json(f"{LLM_BASE_URL}/models", timeout=20, headers=headers)
    except Exception as exc:                           # noqa: BLE001
        problems.append(
            f"LLM endpoint {LLM_BASE_URL} not answering ({str(exc)[:80]}) — every agent "
            f"cron job (Reddit sweeps, morning brief) fails until it is back; tracker "
            f"alerts are unaffected")

# 5. cron jobs that keep failing
try:
    with open(os.path.join(HERMES_HOME, "cron", "jobs.json"), encoding="utf-8") as fh:
        jobs = json.load(fh).get("jobs", [])
except Exception:                                      # noqa: BLE001
    jobs = []
me = os.path.basename(__file__)
for job in jobs:
    if not job.get("enabled") or (job.get("script") or "") == me:
        continue
    streak = job.get("failure_streak") or 0
    if streak >= FAILURE_STREAK:
        problems.append(
            f"cron job '{job.get('name')}' has failed {streak} runs in a row "
            f"({str(job.get('last_error') or job.get('last_status'))[:80]})")

if not problems:
    sys.exit(0)

report = "\n".join(f"- {p}" for p in problems)

# Do not nag: the same report once a day is plenty.
state = {}
try:
    with open(STATE_PATH, encoding="utf-8") as fh:
        state = json.load(fh)
except Exception:                                      # noqa: BLE001
    pass
if state.get("report") == report:
    try:
        if utcnow() - as_utc(state["at"]) < dt.timedelta(hours=REPEAT_HOURS):
            sys.exit(0)
    except Exception:                                  # noqa: BLE001
        pass

pushed = "nowhere"
if engine_up:
    try:
        post_json(f"{BASE}/api/notify", {"title": TITLE, "message": report})
        pushed = "engine channels"
    except Exception as exc:                           # noqa: BLE001
        problems.append(f"(push via engine failed: {str(exc)[:60]})")
if pushed == "nowhere" and NTFY_TOPIC:
    try:
        post_json(NTFY_SERVER, {"topic": NTFY_TOPIC, "title": TITLE, "message": report,
                                "priority": 4, "tags": ["warning"]})
        pushed = "ntfy directly"
    except Exception as exc:                           # noqa: BLE001
        problems.append(f"(push via ntfy failed: {str(exc)[:60]})")

try:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump({"report": report, "at": utcnow().isoformat()}, fh)
except Exception:                                      # noqa: BLE001
    pass

print(f"PRICEWATCH ALERT (pushed via {pushed}):")
print("\n".join(f"- {p}" for p in problems))
sys.exit(0)
