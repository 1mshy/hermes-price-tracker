#!/usr/bin/env python3
"""Upgrade repo-owned shopping jobs without changing schedules or custom work.

Runs before the gateway at container boot. Only known shopping jobs are touched;
upstream's locked update API preserves all fields outside prompt/delivery.
"""
import hashlib
import json
import os
from pathlib import Path
import sys

PROMPTS = Path('/opt/hermes-cron-prompts')
STATE_PATH = Path(os.environ.get('HERMES_HOME', '/opt/data')) / 'cron' / 'shopping_job_templates.json'
POLICY_MARKER = '\n\n<!-- hermes-shopping-quiet-policy -->\n'
POLICY_END = '\n<!-- /hermes-shopping-quiet-policy -->'
POLICY = '''Shopping delivery policy (takes precedence over earlier delivery instructions):
This job delivers locally. Use notify_user only for a new actionable buying
opportunity, after checking list_alert_events. Do not notify on unchanged
prices, expired deals, source/tool failures, no results or routine status.
Continue with working sources and saved watches after a failed source; try one
useful fallback. Return NO_REPLY when there is nothing actionable. Do not send
a second final response to a messaging channel. The independent health
watchdog handles persistent service incidents; do not push them here.
'''
TEMPLATES = {
    'morning-deals-brief': ('morning-deals-brief.txt', {
        '093ce40296f253d85798186e3eee1d353c53ca75f58a7e6b96b2b7e6ebb5785b',
    }),
    'Sunlu AMS heater community sweep': ('sunlu-community-sweep.txt', {
        'e789cc2ce2d4d1a2ffe28e509a99898b51673b2e719093cbb92906bb13403339',
    }),
}


def digest(text):
    return hashlib.sha256(text.strip().encode()).hexdigest()


def reconcile(jobs, update_job, state):
    installed = dict(state)
    for job in jobs:
        job_id = job.get('id')
        name = job.get('name')
        watchdog = Path(job.get('script') or '').name == 'pricewatch_health_watchdog.py'
        if not job_id or (name not in TEMPLATES and not watchdog):
            continue
        updates = {}
        if job.get('deliver') != 'local':
            updates['deliver'] = 'local'
        if name in TEMPLATES:
            filename, legacy_hashes = TEMPLATES[name]
            template = (PROMPTS / filename).read_text().strip()
            raw_prompt = job.get('prompt') or ''
            current = raw_prompt.strip()
            previous = state.get(job_id) or {}
            managed = (hashlib.sha256(raw_prompt.encode()).hexdigest() in legacy_hashes
                       or digest(current) in legacy_hashes
                       or current == template
                       or (previous.get('managed') and digest(current) == previous.get('hash')))
            if managed:
                prompt = template
            else:
                # Custom product selections/instructions stay intact. Replace
                # only this script's own trailing delivery-policy block.
                start = current.find(POLICY_MARKER)
                end = current.find(POLICY_END, start) if start >= 0 else -1
                if end >= 0:
                    current_body = (current[:start] + current[end + len(POLICY_END):]).strip()
                else:
                    current_body = current
                prompt = current_body + POLICY_MARKER + POLICY.strip() + POLICY_END
            if prompt != current:
                updates['prompt'] = prompt
            installed[job_id] = {'hash': digest(prompt), 'managed': managed}
        if updates:
            update_job(job_id, updates)
    return installed


def main():
    sys.path.insert(0, '/opt/hermes')
    from cron.jobs import load_jobs, update_job
    try:
        state = json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        state = {}
    jobs = load_jobs()
    if isinstance(jobs, dict):
        jobs = jobs.get('jobs', [])
    installed = reconcile(jobs, update_job, state)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix('.tmp')
    temporary.write_text(json.dumps(installed, indent=2))
    temporary.replace(STATE_PATH)


if __name__ == '__main__':
    main()
