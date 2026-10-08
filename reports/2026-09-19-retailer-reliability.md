# Retailer reliability investigation — September 19, 2026 (UTC)

The catalog improved from **33/48 to 37/48 stores returning a product price** on this Docker host. The four recovered stores are SUNLU, KB-3D, Memory Express and Target. These are point-in-time storefront observations; an out-of-stock product may still publish a price. No scraper, browser, proxy or API can guarantee that a third-party retailer will always supply data.

## What changed

- Chromium now keeps dedicated anonymous storefront sessions instead of discarding cookies after each page. It saves session state across container restarts, bounds simultaneous navigations to two and retained contexts to eight, and relaunches after a disconnected browser. Its user agent uses the installed Chromium version. No personal browser profile is used.
- `PW_HTTP_PROXY` now reaches Chromium as well as HTTP, including authenticated proxy credentials. A real local proxy integration test verifies routing and authentication; no commercial proxy was purchased or tested.
- Failed browser strategy entries expire after five minutes. Successful hints expire after one day. Legacy permanent blocks are discarded. A missing product, server/rate-limit error or disabled browser no longer poisons the host strategy.
- Challenge detection distinguishes normal loading placeholders and script strings from actual interstitials, including Walmart's identity-verification redirect.
- Walmart retries a successful-but-empty HTTP response in Chromium. Product identifiers and sellers are retained, unrelated embedded prices are rejected, unknown stock stays unknown, and a sold-out item with no published price receives an explicit unavailable result.
- Amazon prefers the main buy-box price, corroborates ambiguous dollar symbols against matching structured currency data, and retries blocked/empty search pages in Chromium. Keepa is a fallback, with an explicit refresh, product/market identity checks and observation timestamps. Missing or stale provider data is rejected.
- eBay now has browser fallback, regional storefronts and API marketplace headers, and accepts product URLs with slugs. The current connection still receives access challenges.
- Structured extraction keeps the chosen product's offers separate from recommendations. Configurator price ranges are not silently treated as exact prices. Renamed Shopify handles recover through same-store redirects; a missing requested variant is not replaced with a cheaper one.
- An optional SerpApi product endpoint fallback is implemented for Amazon US/CA/UK/DE and Walmart US. It requires both `SERPAPI_KEY` and `PW_RETAIL_SERPAPI_ENABLED=true`, requests uncached observations, validates item identity and freshness, and does not retry paid requests automatically. Walmart Canada is never replaced with a US quote. **This provider remains disabled and has not been tested with live credentials.**

## Failed stores: cause and workaround

| Store | Finding | Result / practical route |
|---|---|---|
| SUNLU | Catalog pointed to its corporate site. Its online store also lists an old handle whose `.js`/`.json` URLs return 404 while HTML redirects to the renamed product. | Fixed domain and redirect recovery. Live FilaDC i10 observation: CAD 249.99. |
| KB-3D | Discovery missed `/store/`. The first found item was a configurator range; breadcrumb text was mistaken for the title. | Fixed discovery and extraction. A specific LDO motor returned USD 20.99. |
| Memory Express | Normal homepage loading text contained “Just a moment…”, causing a false challenge detection; product price needs a specific DOM selector. | Fixed. Laptop sample returned CAD 1,399.99 through HTTP and Chromium. |
| Target | Old probe product was unavailable; current product renders its price after JavaScript without JSON-LD. | Updated probe and added a browser reader that waits for the product-price element. AirPods Pro 3 returned USD 199.99. |
| Best Buy US | Missing API key; direct site returned an international country selector, and the tested browser path failed. | Existing Best Buy developer API requires `BESTBUY_API_KEY`. Best Buy Canada is a separate working keyless integration. |
| eBay | US/Canadian public search returned 403 or a challenge, including in Chromium. | Browser and regional handling improved; configure approved Browse API credentials, or test an appropriate configured proxy. Current uncredentialed access remains blocked. |
| Micro Center | Cloudflare challenge through HTTP and Chromium. | Try a configured proxy/approved data service; local headless rendering alone did not resolve access. |
| B&H | Cloudflare challenge on the homepage through both transports. | Same proxy/service route; cannot discover a current product through this connection. |
| Adorama | DataDome challenge through both transports. | Same proxy/service route. No claim that a particular untested provider will succeed. |
| Siboor | Public website and WooCommerce Store API returned Cloudflare challenges. | A reachable WooCommerce endpoint would offer structured product data, but this connection cannot access it. Proxy/service credentials are the next practical dependency. |
| DigiKey | Cloudflare challenge through both transports. | Official Product Information V4 **ProductDetails** is the appropriate API route for live pricing. This integration is not implemented/configured. |
| Mouser | HTTP returned a thin protection page; Chromium returned explicit access denial. | Official Mouser Search API supplies product data and quantity price breaks. This integration is not implemented/configured. |
| Gulf Coast Robotics | Hostname failed DNS resolution. | Now reported as unreachable instead of exhausting discovery attempts. A browser does not repair a missing storefront; use other sellers of the same part. No assertion about the company's business status. |
| LDO Motors | Manufacturer catalog with no retail checkout prices. | Explicitly marked unsupported for direct price tracking. Search its resellers, including KB-3D, West3D and Fabreeko. |
| AliExpress | Search results are usable, but product pages do not provide dependable variant-level tracking. | Remains search-only. Verification now reports that limitation without mistaking an account/order link for a product. |

## Validation

- **377 tests passed locally and in the deployed Docker image**, including expiry/retry behavior, authenticated proxy configuration, browser restart and session persistence, wrong-product rejection, price ranges, regional currency, renamed Shopify handles, API freshness and API-secret redaction.
- Full catalog: **37 OK, 7 BLOCKED, 1 NEEDS-KEY, 2 LIMITED, 1 UNREACHABLE**. See `2026-09-19-retailer-deployed.json` for the final installed-code results; `2026-09-19-retailer-catalog-predeploy.json` retains an earlier run before the last Amazon currency correction.
- Repeated matrix: **24/26 normal price reads succeeded** across 13 product URLs and eight retailers. All three Amazon samples succeeded twice; three of four Walmart samples succeeded twice, including a CAD 19.88 Walmart Canada item. The remaining Walmart product (500903542) is explicitly `OUT_OF_STOCK` with `currentPrice: null`, confirmed in its live product payload. It cannot honestly return a current price.
- Forced-browser matrix: **6/8 price reads succeeded**. The unavailable Walmart item has no price, and the Walmart Canada browser path encountered an identity-verification interstitial while normal HTTP worked. This demonstrates why both transports are retained.
- Target additionally passed a live Chromium lookup after its dedicated reader was added.
- Real Chromium recovery: session cookie reuse, JavaScript price rendering, browser relaunch after an actual browser shutdown, disk-state restoration in a new fetcher, and absence of leaked tabs all passed. This is browser/process recovery, **not a physical laptop lid-close test**.
- Local authenticated proxy integration: see `2026-09-19-proxy-integration.json`.
- No paid API calls, purchases, carts, account creation or notification messages were made by these retailer tests. The previous notification and Docker/host recovery tests are documented separately in `2026-09-18-reliability.md`.

Deployment rebuilt the price engine, verified that installed source matches the working tree, and reconnected Hermes. Both services are healthy; the running tracker checked all seven existing listings successfully with no alerts. Paid retailer fallback and commercial proxy settings remain disabled/unconfigured.

The initial HTTP/Chromium diagnosis and both matrix passes are retained as raw JSON. Earlier passes intentionally retain failures that led to subsequent fixes; they are not overwritten as successes. A final deployed spot check confirmed Amazon.com’s converted CAD 140.07 observation is labelled CAD, and the unavailable Walmart item is classified as `http:unavailable` with stock false and no invented price.

## Optional routes and configuration

The default remains direct HTTP plus Chromium. To enable the managed product fallback, configure `SERPAPI_KEY` and `PW_RETAIL_SERPAPI_ENABLED=true`, then rebuild/recreate the service. Each fallback can consume provider credits; the implementation disables the provider's normal cache. Keepa's `update=0` can consume an additional token. Both integrations expose observation time and price basis, and neither is equivalent to a guaranteed checkout total.

To route both transports through an existing proxy, configure `PW_HTTP_PROXY` with its endpoint and credentials and recreate the service. Proxy access to the remaining retailers must still be verified using that provider; the local proxy integration only proves the application's routing/authentication behavior.

Primary documentation consulted:

- [Playwright browser contexts and storage state](https://playwright.dev/python/docs/api/class-browsercontext) and [proxy configuration](https://playwright.dev/python/docs/network).
- [Keepa product requests, refresh behavior and token usage](https://keepa.com/api-docs/product.html) and [product identity, timestamps and currency units](https://keepa.com/api-docs/product-object.html).
- [SerpApi Amazon product API](https://serpapi.com/amazon-product-api) and [Walmart product API](https://serpapi.com/walmart-product-api).
- [eBay Browse item API](https://developer.ebay.com/api-docs/buy/browse/resources/item/methods/getItem).
- [DigiKey Product Information V4](https://developer.digikey.com/products/product-information-v4/productsearch) and [DigiKey freshness guidance](https://developer.digikey.com/faq/products-plans-and-apis): keyword search may be cached; ProductDetails is recommended for current pricing.
- [Mouser Search API](https://www.mouser.com/en/api-search/).

Amazon's old Product Advertising API documentation now points users toward [Creators API migration](https://affiliate-program.amazon.com/creatorsapi/docs/en-us/migrating-to-creatorsapi-from-paapi). That affiliate-program integration has eligibility and credential requirements and is not configured here. Keepa and SerpApi are third-party data providers, not Amazon's official API.
