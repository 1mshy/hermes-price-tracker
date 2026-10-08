#!/usr/bin/env python3
"""Read-only live retailer matrix plus real Chromium session/crash recovery.

Run with pricewatch installed, or PYTHONPATH=pricewatch/src. Supply --output.
Uses isolated temporary browser state, disables paid APIs and never edits watches.
A successful sample is an observation, not a guarantee of future availability.
"""
import argparse
import asyncio
import datetime as dt
import json
import re
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin

from pricewatch.fetch import Fetcher, Page, fetcher
from pricewatch.settings import settings
from pricewatch.stores.catalog import BY_KEY
from pricewatch.stores.registry import ADAPTERS, fetch_offer
from pricewatch.verify import discover_product_url


async def browser_recovery():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            if self.path == '/set':
                self.send_header('Set-Cookie', 'market=CAD; Path=/; Max-Age=3600')
            self.end_headers()
            body = '<title>Browser recovery test</title><p id="price"></p><script>document.querySelector("#price").textContent="499.00"</script>'
            body += '<p id="cookie">' + self.headers.get('Cookie', '') + '</p>'
            self.wfile.write(body.encode())
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root = f'http://127.0.0.1:{server.server_port}'
    browser = Fetcher()
    checks = {}
    try:
        await browser.render(root + '/set')
        page = await browser.render(root + '/read')
        checks['session_cookie_reused'] = 'market=CAD' in page.text
        checks['javascript_price_rendered'] = '<p id="price">499.00</p>' in page.text
        await browser._browser.close()  # abrupt browser loss, like a failed wake
        page = await browser.render(root + '/read')
        checks['browser_relaunched_and_cookie_restored'] = 'market=CAD' in page.text
        await browser.aclose()
        browser = Fetcher()
        page = await browser.render(root + '/read')
        checks['new_fetcher_restores_disk_session'] = 'market=CAD' in page.text
        checks['no_leaked_pages'] = all(not c.pages for c in browser._contexts.values())
    finally:
        await browser.aclose()
        server.shutdown()
        server.server_close()
    return checks


async def main(args):
    settings.pw_playbook_path = 'off'
    fetcher.playbook._data.clear()
    settings.pw_retail_serpapi_enabled = False
    settings.keepa_api_key = ''
    settings.bestbuy_api_key = ''
    settings.ebay_app_id = settings.ebay_cert_id = ''
    rows, discovery = [], []
    with tempfile.TemporaryDirectory(prefix='retailer-sessions-') as sessions:
        settings.pw_browser_state_dir = sessions
        urls = []
        for key in args.store or ['amazon', 'walmart', 'bestbuyca', 'canadacomputers', 'newegg', 'sunlu', 'kb3d', 'memoryexpress']:
            try:
                url, how = await asyncio.wait_for(discover_product_url(BY_KEY[key], True), 55)
                discovery.append({'store': key, 'url': url, 'method': how})
                if url:
                    urls.append((key, url))
            except Exception as exc:
                discovery.append({'store': key, 'error': type(exc).__name__})
        # Sample more than one product on the major marketplaces.
        if any(k == 'amazon' for k, _ in urls):
            hits = await asyncio.wait_for(ADAPTERS['amazon'].search('Logitech MX Master 3S', limit=2), 55)
            urls.extend(('amazon', hit.url) for hit in hits)
        if any(k == 'walmart' for k, _ in urls):
            seed = next(u for k, u in urls if k == 'walmart')
            page = await fetcher.get(seed)
            links = re.findall(r'(?:href=|["\']canonicalUrl["\']:)\s*["\']([^"\']*/ip/[^"\']+)["\']', page.text)
            seen = {re.search(r'(\d+)(?:\?|$)', seed).group(1)}
            for link in links:
                clean = urljoin(seed, link).split('?')[0]
                item = ADAPTERS['walmart']._item_id(clean)
                if item and item.isdigit() and item not in seen and 'walmart.com/' in clean:
                    seen.add(item)
                    urls.append(('walmart', clean))
                    if len(seen) == 3:
                        break
        for url in args.url or []:
            from pricewatch.stores.registry import store_key_for_url
            key = store_key_for_url(url)
            if key:
                urls.append((key, url))
        urls = list(dict.fromkeys(urls))
        async def observe(key, url, phase, iteration):
            started = time.monotonic()
            try:
                result = await asyncio.wait_for(fetch_offer(url), 60)
                row = {'store': key, 'url': url, 'phase': phase, 'iteration': iteration,
                       **result.as_dict()}
            except Exception as exc:
                row = {'store': key, 'url': url, 'phase': phase, 'iteration': iteration,
                       'error': type(exc).__name__}
            row['seconds'] = round(time.monotonic() - started, 2)
            rows.append(row)
            print(json.dumps(row), flush=True)
        try:
            for iteration in range(1, args.repeat + 1):
                for key, url in urls:
                    await observe(key, url, 'normal', iteration)
            original_get = fetcher.get
            async def force_browser(url, **kwargs):
                return Page(url, 403, 'Browser-only integration test', 'http')
            fetcher.get = force_browser
            fetcher.playbook._data.clear()
            try:
                for key, url in urls:
                    if key in ('amazon', 'walmart', 'memoryexpress'):
                        await observe(key, url, 'forced-browser', 1)
            finally:
                fetcher.get = original_get
            recovery = await browser_recovery()
            print(json.dumps({'browser_recovery': recovery}), flush=True)
        finally:
            await fetcher.aclose()
    report = {'observed_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'paid_apis': False,
              'discovery': discovery, 'browser_recovery': recovery, 'results': rows}
    Path(args.output).write_text(json.dumps(report, indent=2))
    return 0 if all(recovery.values()) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--url', action='append', help='additional public product URL')
    parser.add_argument('--repeat', type=int, default=2)
    parser.add_argument('--store', action='append', choices=list(BY_KEY))
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error('--repeat must be positive')
    raise SystemExit(asyncio.run(main(args)))
