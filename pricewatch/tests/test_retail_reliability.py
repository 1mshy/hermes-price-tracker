"""Regression coverage for live retailer failures, browser recovery and API fallback."""
import asyncio
import datetime as dt
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from pricewatch import fetch as f
from pricewatch.extract import extract
from pricewatch.stores import bigbox, registry, retail_api
from pricewatch.stores.base import StoreResult


def test_loading_placeholder_and_javascript_are_not_challenges():
    body = '<title>Memory Express</title>' + '<p>Product</p>' * 1500
    body += '<div class="loading">Just a moment...</div><script>const error="access denied";</script>'
    assert not f.Page('https://memoryexpress.com/', 200, body, 'http').looks_blocked
    assert f.Page('https://mouser.com/', 200, '<p>Powered and protected by</p>', 'http').looks_blocked


def test_playbook_expires_block_and_migrates_legacy(monkeypatch, tmp_path):
    path = tmp_path / 'playbook.json'
    path.write_text(json.dumps({'legacy.com': 'blocked'}))
    monkeypatch.setattr(f.settings, 'pw_playbook_path', str(path))
    clock = [1000]
    monkeypatch.setattr(f.time, 'time', lambda: clock[0])
    book = f._Playbook()
    assert book.strategy('legacy.com') is None
    book.record('www.example.com', f.BLOCKED_STRAT)
    assert f._Playbook().strategy('example.com') == f.BLOCKED_STRAT
    clock[0] += f.settings.pw_blocked_retry_seconds + 1
    assert book.strategy('example.com') is None
    assert book.as_dict() == {}


def test_expired_block_retries_browser(monkeypatch):
    monkeypatch.setattr(f.settings, 'pw_blocked_retry_seconds', 0)
    fetch = f.Fetcher()
    fetch.playbook.record('shop.example', f.BLOCKED_STRAT)
    calls = []
    async def get(url):
        return f.Page(url, 403, 'challenge', 'http')
    async def render(url, **kwargs):
        calls.append(url)
        return f.Page(url, 200, 'product' * 500, 'browser')
    monkeypatch.setattr(fetch, 'get', get)
    monkeypatch.setattr(fetch, 'render', render)
    result = asyncio.run(fetch.get_or_render('https://shop.example/p/1'))
    assert result.method == 'browser' and len(calls) == 1
    assert fetch.playbook.strategy('shop.example') == f.BROWSER


@pytest.mark.parametrize('status', [404, 410, 429, 503])
def test_dead_product_or_transient_failure_does_not_block_host(monkeypatch, status):
    fetch = f.Fetcher()
    async def get(url):
        return f.Page(url, status, 'x' * 3000, 'http')
    async def render(url, **kwargs):
        assert status not in (404, 410)
        return f.Page(url, status, 'x' * 3000, 'browser')
    monkeypatch.setattr(fetch, 'get', get)
    monkeypatch.setattr(fetch, 'render', render)
    asyncio.run(fetch.get_or_render('https://example.com/p/retired'))
    assert fetch.playbook.strategy('example.com') is None


def test_disabled_browser_does_not_poison_playbook(monkeypatch):
    monkeypatch.setattr(f.settings, 'pw_browser_enabled', False)
    fetch = f.Fetcher()
    async def get(url):
        return f.Page(url, 403, 'challenge', 'http')
    monkeypatch.setattr(fetch, 'get', get)
    with pytest.raises(f.Blocked):
        asyncio.run(fetch.get_or_render('https://example.com/p/1'))
    assert fetch.playbook.strategy('example.com') is None


def test_proxy_auth_reaches_chromium_without_credentials_in_server(monkeypatch):
    monkeypatch.setattr(f.settings, 'pw_http_proxy', 'http://user%40name:p%3Ass@proxy.example:8080')
    assert f.Fetcher._browser_proxy() == {'server': 'http://proxy.example:8080',
                                        'username': 'user@name', 'password': 'p:ss'}


class FakePage:
    def __init__(self, context):
        self.context = context
        self.url = ''
    async def goto(self, url, **kwargs):
        self.url = url
        return SimpleNamespace(status=200)
    async def wait_for_timeout(self, delay):
        pass
    async def content(self):
        return '<html>product</html>'
    async def close(self):
        self.context.pages.remove(self)


class FakeContext:
    def __init__(self):
        self.pages = []
        self.closed = False
    async def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page
    async def storage_state(self):
        return {'cookies': [], 'origins': []}
    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.version = "153.0.0.1"
        self.contexts = []
        self.connected = True
        self.options = []
    def is_connected(self):
        return self.connected
    async def new_context(self, **kwargs):
        self.options.append(kwargs)
        context = FakeContext()
        self.contexts.append(context)
        return context
    async def close(self):
        self.connected = False


def test_browser_reuses_session_bounds_contexts_and_restores_saved_state(monkeypatch, tmp_path):
    monkeypatch.setattr(f.settings, 'pw_browser_enabled', True)
    monkeypatch.setattr(f.settings, 'pw_browser_context_limit', 1)
    monkeypatch.setattr(f.settings, 'pw_browser_state_dir', str(tmp_path))
    monkeypatch.setattr(f.settings, 'pw_per_host_rps', 0)
    fetch = f.Fetcher()
    browser = FakeBrowser()
    async def ensure():
        return browser
    monkeypatch.setattr(fetch, '_ensure_browser', ensure)
    async def scenario():
        await fetch.render('https://one.example/p/1')
        await fetch.render('https://one.example/p/2')
        assert len(browser.contexts) == 1 and browser.contexts[0].pages == []
        await fetch.render('https://two.example/p/1')
        assert browser.contexts[0].closed
        await fetch.render('https://one.example/p/3')
        assert browser.options[-1]['storage_state'] == {'cookies': [], 'origins': []}
        assert 'Chrome/153.0.0.1' in browser.options[-1]['user_agent']
    asyncio.run(scenario())
    assert len(list(tmp_path.glob('*.json'))) == 2
    assert all(p.stat().st_mode & 0o077 == 0 for p in tmp_path.glob('*.json'))


def test_disconnected_browser_relaunches_with_proxy(monkeypatch):
    fetch = f.Fetcher()
    fetch._browser = FakeBrowser()
    fetch._browser.connected = False
    fetch._contexts['old'] = FakeContext()
    launched = []
    async def launch(**kwargs):
        launched.append(kwargs)
        return FakeBrowser()
    fetch._playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    monkeypatch.setattr(f.settings, 'pw_http_proxy', 'http://proxy.example:8080')
    browser = asyncio.run(fetch._ensure_browser())
    assert browser.is_connected() and fetch._contexts == {}
    assert launched[0]['proxy']['server'] == 'http://proxy.example:8080'


def test_recommended_product_cannot_supply_main_product_price():
    raw = '<h1>Main printer</h1><script type="application/ld+json">' + json.dumps([
        {'@type': 'Product', 'name': 'Cheap cable', 'url': 'https://shop/p/cable',
         'offers': {'price': 3, 'priceCurrency': 'USD'}},
        {'@type': 'Product', 'name': 'Main printer', 'url': 'https://shop/p/printer',
         'offers': {'price': 499, 'priceCurrency': 'USD'}}]) + '</script>'
    result = extract(raw, url='https://shop/p/printer')
    assert result.price == Decimal('499') and result.title == 'Main printer'
    assert not extract('<script>{"currentPrice": 499, "recommendations": [{"currentPrice": 3}]}</script>').ok


def walmart_html(item='123456789', stock=None, price=199):
    product = {'name': 'Headphones', 'usItemId': item,
               'priceInfo': {'currentPrice': {'price': price, 'currencyUnit': 'USD'}}}
    if stock is not None:
        product['availabilityStatus'] = stock
    data = {'props': {'pageProps': {'initialData': {'data': {'product': product}}}}}
    return '<script type="application/json" id="__NEXT_DATA__">' + json.dumps(data) + '</script>'


def test_walmart_200_shell_escalates_and_unknown_stock_stays_unknown(monkeypatch):
    url = 'https://www.walmart.com/ip/123456789'
    async def get(*args, **kwargs):
        return f.Page(url, 200, '<html>app shell</html>', 'http')
    async def render(*args, **kwargs):
        return f.Page(url, 200, walmart_html(), 'browser')
    monkeypatch.setattr(bigbox.fetcher, 'get_or_render', get)
    monkeypatch.setattr(bigbox.fetcher, 'render', render)
    monkeypatch.setattr(f.settings, 'pw_browser_enabled', True)
    result = asyncio.run(bigbox.WalmartAdapter().fetch_offer(url))
    assert result.price == Decimal('199') and result.method == 'browser:next-data'
    assert result.in_stock is None


def test_walmart_rejects_wrong_variant_challenge_and_recommendation_prices():
    url = 'https://www.walmart.com/ip/123456789'
    adapter = bigbox.WalmartAdapter()
    for page in [f.Page(url, 200, walmart_html(item='987654321'), 'http'),
                 f.Page(url, 403, walmart_html(), 'http'),
                 f.Page(url, 200, '<script>{"recommended":{"currentPrice":2}}</script>', 'http')]:
        result = adapter._parse(page, url)
        assert result is None or not result.ok


def provider_data(store='walmart'):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    result = {'search_metadata': {'processed_at': now, 'status': 'Success'}}
    if store == 'walmart':
        result['product_result'] = {'us_item_id': '123456789', 'title': 'Headphones',
                                    'price_map': {'price': 199, 'currency': 'USD'}}
    else:
        result.update(product_results={'title': 'Headphones', 'price': 'CA$199.00', 'extracted_price': 199},
                      product_details={'asin': 'B012345678'})
    return result


def test_provider_requires_explicit_opt_in_and_preserves_market(monkeypatch):
    monkeypatch.setattr(f.settings, 'serpapi_key', 'secret-test-key')
    monkeypatch.setattr(f.settings, 'pw_retail_serpapi_enabled', False)
    assert asyncio.run(retail_api.fallback('https://walmart.com/ip/123456789')) is None
    assert retail_api._request('https://www.walmart.ca/en/ip/test/123456789') is None
    assert retail_api._request('https://www.amazon.ca/dp/B012345678')[2]['amazon_domain'] == 'amazon.ca'


def test_provider_rejects_stale_mismatched_or_range_quotes():
    data = provider_data()
    assert retail_api._parse(data, 'https://walmart.com/ip/123456789', 'walmart', '123456789').ok
    assert not retail_api._parse(data, 'https://walmart.com/ip/999999999', 'walmart', '999999999').ok
    data['search_metadata']['processed_at'] = '2020-01-01T00:00:00+00:00'
    assert not retail_api._parse(data, 'https://walmart.com/ip/123456789', 'walmart', '123456789').ok
    data = provider_data('amazon')
    result = retail_api._parse(data, 'https://amazon.ca/dp/B012345678', 'amazon', 'B012345678')
    assert result.ok and result.currency == 'CAD'
    data['product_results']['extracted_max_price'] = 299
    assert not retail_api._parse(data, result.url, 'amazon', 'B012345678').ok


def test_provider_requests_uncached_product_and_redacts_failure(monkeypatch):
    monkeypatch.setattr(f.settings, 'serpapi_key', 'secret-test-key')
    monkeypatch.setattr(f.settings, 'pw_retail_serpapi_enabled', True)
    calls = []
    async def get(url, **kwargs):
        calls.append(kwargs)
        raise RuntimeError('https://provider/?api_key=secret-test-key')
    async def client():
        return SimpleNamespace(get=get)
    monkeypatch.setattr(retail_api.fetcher, 'client', client)
    result = asyncio.run(retail_api.fallback('https://walmart.com/ip/123456789'))
    assert calls[0]['params']['no_cache'] == 'true'
    assert 'secret-test-key' not in result.error


def test_registry_uses_provider_only_after_failed_direct_read(monkeypatch):
    calls = []
    async def direct(url):
        return StoreResult(store='walmart', url=url, error='challenge')
    async def alternate(url):
        calls.append(url)
        return StoreResult(store='walmart', url=url, price=Decimal('199'), method='serpapi:product')
    monkeypatch.setattr(registry.ADAPTERS['walmart'], 'fetch_offer', direct)
    monkeypatch.setattr(retail_api, 'fallback', alternate)
    result = asyncio.run(registry.fetch_offer('https://walmart.com/ip/123456789'))
    assert result.ok and len(calls) == 1 and result.extra['direct_fetch_error'] == 'challenge'


def test_microdata_uses_main_title_and_does_not_collapse_price_range():
    raw = ('<span itemprop="name">Home</span><h1>Printer kit</h1>'
           '<div itemscope itemtype="https://schema.org/Offer">'
           '<meta itemprop="price" content="41.81 - 164.99">'
           '<meta itemprop="priceCurrency" content="USD"></div>'
           '<article><span itemprop="price">3.99</span></article>')
    assert not extract(raw).ok
    found = extract(raw.replace('41.81 - 164.99', '164.99'))
    assert found.price == Decimal('164.99') and found.title == 'Printer kit'


def test_shopify_renamed_handle_recovers_and_preserves_selected_variant(monkeypatch):
    from pricewatch.stores import shopify
    calls = []
    async def get_json(url, **kwargs):
        calls.append(url)
        if '/old.' in url:
            raise RuntimeError('HTTP 404')
        if url.endswith('/new.js'):
            return {'title': 'Renamed product', 'variants': [{'id': 7, 'price': 9900, 'available': True}]}
        return {'currency': 'CAD'}
    async def get(url, **kwargs):
        return f.Page('https://store.example/en-ca/products/new', 200, '<html>product</html>', 'http')
    monkeypatch.setattr(shopify.fetcher, 'get_json', get_json)
    monkeypatch.setattr(shopify.fetcher, 'get', get)
    result = asyncio.run(shopify.ShopifyAdapter().fetch_offer('https://store.example/en-ca/products/old?variant=7'))
    assert result.ok and result.price == Decimal('99') and result.currency == 'CAD'
    assert result.url == 'https://store.example/en-ca/products/new?variant=7'
    assert 'https://store.example/en-ca/products/new.js' in calls


def test_keepa_rejects_stale_and_wrong_market(monkeypatch):
    from pricewatch.stores import apis
    item = {'asin': 'B012345678', 'domainId': 6, 'productType': 0,
            'lastUpdate': int(apis.time.time() / 60) - 21564000 - 120,
            'stats': {'current': [19999, -1]}}
    async def get_json(url, **kwargs):
        assert 'update=0' in url
        return {'products': [item]}
    monkeypatch.setattr(apis.settings, 'keepa_api_key', 'test')
    monkeypatch.setattr(apis.fetcher, 'get_json', get_json)
    assert asyncio.run(apis.KeepaAmazon.lookup('B012345678', host='amazon.ca')) is None
    item['lastUpdate'] += 120
    assert asyncio.run(apis.KeepaAmazon.lookup('B012345678', host='amazon.com')) is None
    result = asyncio.run(apis.KeepaAmazon.lookup('B012345678', host='amazon.ca'))
    assert result.ok and result.in_stock is None and result.extra['observed_at']


def test_ebay_slug_links_preserve_canadian_currency():
    from pricewatch.stores.apis import _parse_ebay_search
    raw = ('<li class="s-item"><a href="https://www.ebay.ca/itm/product-slug/123456789012">Item</a>'
           '<h3 class="s-item__title">Printer</h3><span class="s-item__price">$99.00</span></li>')
    result = _parse_ebay_search(raw, host='ebay.ca')[0]
    assert result.currency == 'CAD' and 'ebay.ca' in result.url


def test_amazon_main_buybox_beats_recommendation_json():
    raw = ('<script>{"priceAmount":3,"currencySymbol":"$"}</script>'
           '<span id="productTitle">Main product</span>'
           '<div id="corePriceDisplay_desktop_feature_div"><span class="a-price">'
           '<span class="a-offscreen">$199.00</span></span></div>')
    result = bigbox.AmazonAdapter()._parse('https://amazon.com/dp/B012345678', raw, 'B012345678')
    assert result.price == Decimal('199')


def test_target_waits_for_product_price_ignoring_installments(monkeypatch):
    async def page(url, **kwargs):
        assert kwargs['browser_first'] and kwargs['wait_for'] == "[data-test='product-price']"
        return f.Page(url, 200, '<h1 data-test="product-title">AirPods</h1>'
                      '<p>$24/mo</p><span data-test="product-price">$199.99</span>', 'browser')
    monkeypatch.setattr(bigbox.fetcher, 'get_or_render', page)
    result = asyncio.run(registry.ADAPTERS['target'].fetch_offer('https://www.target.com/p/-/A-85978609'))
    assert result.price == Decimal('199.99') and result.title == 'AirPods'


def test_api_query_credentials_are_redacted():
    url = f._safe_url('https://api.example/product?apiKey=secret&sku=123')
    assert 'secret' not in url and 'sku=123' in url


def test_walmart_unavailable_item_keeps_stock_evidence_without_guessing_price(monkeypatch):
    url = 'https://walmart.com/ip/123456789'
    async def get(*args, **kwargs):
        return f.Page(url, 200, walmart_html(stock='OUT_OF_STOCK', price=None), 'http')
    async def render(*args, **kwargs):
        pytest.fail('A confirmed unavailable item should not trigger another browser request')
    monkeypatch.setattr(bigbox.fetcher, 'get_or_render', get)
    monkeypatch.setattr(bigbox.fetcher, 'render', render)
    result = asyncio.run(bigbox.WalmartAdapter().fetch_offer(url))
    assert result.in_stock is False and result.price is None and 'out of stock' in result.error


def test_walmart_identity_interstitial_is_classified_as_blocked():
    assert f.Page('https://www.walmart.ca/blocked?url=abc', 200,
                  '<title>Verify Your Identity</title>', 'browser').looks_blocked


def test_amazon_dom_amount_keeps_matching_converted_currency():
    raw = ('<script>{"priceAmount":140.07,"currencySymbol":"CAD"}</script>'
           '<script>{"priceAmount":3,"currencySymbol":"$"}</script>'
           '<span id="productTitle">Main product</span>'
           '<div id="corePriceDisplay_desktop_feature_div"><span class="a-price">'
           '<span class="a-offscreen">$140.07</span></span></div>')
    result = bigbox.AmazonAdapter()._parse('https://amazon.com/dp/B012345678', raw, 'B012345678')
    assert result.price == Decimal('140.07') and result.currency == 'CAD'
