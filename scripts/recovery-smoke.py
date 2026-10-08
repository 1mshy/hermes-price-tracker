#!/usr/bin/env python3
"""Exercise real Docker freeze/resume and restart with isolated state and local stores.

Run: python3 scripts/recovery-smoke.py [--report /tmp/recovery-report.json]
No production volumes, store requests, or real notification recipients are used.
The worker uses the installed dependencies, current source, real SQLite, scheduler,
Shopify adapter, HTTP notification transport, and a deliberately failing relay.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid


def worker():
    import asyncio
    from decimal import Decimal
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    root = Path('/evidence')
    os.environ.update(PW_DB_URL='sqlite:////evidence/pricewatch.db', PW_PLAYBOOK_PATH='off',
                      PW_BROWSER_ENABLED='false', PW_FX_ENABLED='false',
                      PW_PREFERRED_CURRENCY='CAD', PW_REGION='CA', PW_PER_HOST_RPS='20',
                      NTFY_TOPIC='isolated-test', NTFY_SERVER='http://127.0.0.1:8765')
    from apscheduler.triggers.interval import IntervalTrigger
    from pricewatch import scheduler, service, notify
    from pricewatch.db import init_db, session_scope
    from pricewatch.models import Offer, Product, Tracker, AlertEvent
    from pricewatch.stores.shopify import ShopifyAdapter
    from pricewatch.stores.base import StoreResult
    from pricewatch.fetch import fetcher

    def event(kind, **data):
        with (root / 'events.jsonl').open('a') as out:
            out.write(json.dumps({'kind': kind, 'at': time.time(), **data}) + '\n')

    class StoreAndRelay(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def respond(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            if self.path == '/cart.js':
                self.respond(200, {'currency': 'CAD'})
            elif self.path == '/products/recovery.js':
                price = int((root / 'price').read_text())
                self.respond(200, {'id': 1, 'title': 'Recovery test product',
                                   'variants': [{'id': 1, 'price': price, 'available': True}]})
            else:
                self.respond(404, {})
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            failed = root / 'relay-failed-once'
            if not failed.exists():
                failed.touch()
                event('notification', status=503)
                self.respond(503, {'error': 'simulated wake outage'})
            else:
                event('notification', status=200, title=payload['title'])
                self.respond(200, {'id': 'accepted'})

    server = ThreadingHTTPServer(('127.0.0.1', 8765), StoreAndRelay)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    init_db()
    url = 'http://127.0.0.1:8765/products/recovery'
    with session_scope() as session:
        if session.query(Product).count() == 0:
            product = Product(title='Recovery test product', match_key='recovery')
            session.add(product)
            session.flush()
            service._upsert_offer(session, product.id, StoreResult(
                store='local', url=url, title=product.title, price=Decimal('20'),
                currency='CAD', in_stock=True, method='seed'))
            session.add(Tracker(product_id=product.id, currency='CAD', target_price=Decimal('10')))

    service.fetch_offer = ShopifyAdapter().fetch_offer
    notify.RETRY_DELAYS = (0.1, 0.2)
    original_refresh = service.refresh_offers
    async def refresh(*args, **kwargs):
        result = await original_refresh(*args, **kwargs)
        with session_scope() as session:
            offer = session.query(Offer).one()
            event('sweep', price=str(offer.last_price), checked=result['checked'],
                  failed=result['failed'], alerts=len(result['alerts']),
                  delivered=session.query(AlertEvent).filter_by(delivered=True).count())
        return result
    service.refresh_offers = refresh

    async def run():
        scheduler.start()
        # Shortened cadence only in this isolated smoke process. All scheduling
        # options (coalescing/misfire behavior) come from production start().
        scheduler.scheduler.reschedule_job(scheduler._JOB_ID, trigger=IntervalTrigger(seconds=5))
        event('boot')
        try:
            await asyncio.Event().wait()
        finally:
            scheduler.stop()
            await fetcher.aclose()
    asyncio.run(run())


def controller(report_path):
    repo = Path(__file__).resolve().parents[1]
    name = 'hermes-recovery-test-' + uuid.uuid4().hex[:8]
    def docker(*args, timeout=60):
        result = subprocess.run(['docker', *args], text=True, capture_output=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return result.stdout.strip()
    with tempfile.TemporaryDirectory(prefix='hermes-recovery-') as scratch:
        root = Path(scratch)
        (root / 'price').write_text('2000')
        def events():
            path = root / 'events.jsonl'
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        def wait_for(predicate, seconds=30):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                rows = events()
                if predicate(rows):
                    return rows
                time.sleep(0.1)
            raise AssertionError('Timed out waiting for recovery evidence: ' + str(events()))
        report = {}
        try:
            docker('run', '-d', '--name', name, '--network', 'none',
                   '-v', f'{repo}:/source:ro', '-v', f'{root}:/evidence',
                   '-e', 'PYTHONPATH=/source/pricewatch/src', '--entrypoint', 'python',
                   'hermes-shopping/pricewatch:1.0.0', '/source/scripts/recovery-smoke.py', '--worker')
            wait_for(lambda rows: any(r['kind'] == 'sweep' for r in rows))
            report['startup_catchup'] = True
            docker('pause', name)
            before = events()
            (root / 'price').write_text('900')
            time.sleep(12)  # multiple missed 5-second ticks
            assert events() == before, 'worker did work while paused'
            resumed = time.time()
            docker('unpause', name)
            rows = wait_for(lambda rows: any(r['kind'] == 'sweep' and r.get('delivered') == 1
                                            for r in rows))
            catchup = [r for r in rows if r['kind'] == 'sweep' and r['at'] >= resumed]
            assert len(catchup) == 1, 'missed ticks were not coalesced'
            assert catchup[0]['price'] == '9.00' and catchup[0]['failed'] == 0
            assert catchup[0]['at'] - resumed < 4, 'waited for normal cadence instead of catching up'
            statuses = [r['status'] for r in rows if r['kind'] == 'notification']
            assert statuses == [503, 200], statuses
            report.update(freeze_seconds=12, catchup_seconds=round(catchup[0]['at'] - resumed, 2),
                          missed_ticks_coalesced=True, notification_retry=statuses)
            # Restart loses the scheduler's memory. SQLite must retain the alert
            # watermark and startup must immediately refresh the listing again.
            old_boots = sum(r['kind'] == 'boot' for r in rows)
            docker('restart', '--time', '1', name)
            rows = wait_for(lambda rows: sum(r['kind'] == 'boot' for r in rows) > old_boots
                            and any(r['kind'] == 'sweep' and r['at'] > max(
                                e['at'] for e in rows if e['kind'] == 'boot') for r in rows))
            latest = [r for r in rows if r['kind'] == 'sweep'][-1]
            assert latest['price'] == '9.00' and latest['alerts'] == 0 and latest['delivered'] == 1
            assert [r['status'] for r in rows if r['kind'] == 'notification'] == [503, 200]
            report.update(restart_catchup=True, persisted_alert_deduplication=True, events=rows)
            if report_path:
                Path(report_path).write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps({k: v for k, v in report.items() if k != 'events'}, indent=2))
        except Exception:
            print(docker('logs', name), file=sys.stderr)
            raise
        finally:
            subprocess.run(['docker', 'rm', '-f', name], capture_output=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--report')
    args = parser.parse_args()
    worker() if args.worker else controller(args.report)
