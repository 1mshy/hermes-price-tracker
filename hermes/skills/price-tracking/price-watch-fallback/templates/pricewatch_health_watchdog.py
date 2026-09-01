#!/usr/bin/env python3
"""Silent engine-health watchdog (no_agent cron job).

Prints ONLY when something is wrong — engine down, or a tracked listing that
has failed 3+ consecutive sweeps (which means an alert could be silently
missed). Empty stdout = healthy = no message (the watchdog pattern).

Install: copy to $HERMES_HOME/scripts/, then
    cronjob action=create no_agent=true script=pricewatch_health_watchdog.py schedule='6h'
"""
import json
import os
import sys
import urllib.request

BASE = os.environ.get("PRICEWATCH_BASE", "http://pricewatch:8000")
MAX_CONSECUTIVE_ERRORS = 3

problems = []

try:
    health = json.load(urllib.request.urlopen(f"{BASE}/api/health", timeout=15))
    if health.get("status") != "ok":
        problems.append(f"engine health check returned: {health}")
except Exception as exc:                               # noqa: BLE001
    print(f"PRICEWATCH ALERT: price engine unreachable ({exc}). "
          "Tracked prices are NOT being checked.")
    sys.exit(0)

try:
    trackers = json.load(
        urllib.request.urlopen(f"{BASE}/api/trackers", timeout=30))["trackers"]
except Exception as exc:                               # noqa: BLE001
    print(f"PRICEWATCH ALERT: could not list trackers ({exc}).")
    sys.exit(0)

for tracker in trackers:
    if not tracker.get("active"):
        continue
    for offer in tracker.get("offers", []):
        errors = offer.get("consecutive_errors") or 0
        if errors >= MAX_CONSECUTIVE_ERRORS:
            problems.append(
                f"'{tracker.get('label', '?')}' at {offer.get('store')} has "
                f"failed {errors} sweeps in a row "
                f"({str(offer.get('error'))[:80]}) — {offer.get('url')}")

if problems:
    print("PRICEWATCH ALERT: tracked listings need attention:")
    for problem in problems:
        print(f"- {problem}")
sys.exit(0)
