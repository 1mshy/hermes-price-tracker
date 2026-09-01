---
name: price-watch-fallback
description: Read prices and build silent price alerts when the pricewatch engine can't reach a store (bot challenges, missing API keys). Amazon fallback with direct curl + parse patterns, and the no_agent cron watchdog pattern.
metadata:
  version: 1.0.0
---

# Price fallbacks and silent watchdogs

Use when `pricewatch` tools fail on a URL (e.g. `browser:failed` / "bot
challenge" from `get_price`, `track_product_url`, `compare_prices`) or a store
is unsearchable / missing its API key (check `engine_status` first — it tells
you which optional keys are absent and why a store fails). Do not report the
item as unpriceable when a direct fetch would work.

## 1. Direct price fetch (Amazon, works from this host)

The engine's headless browser gets bot-challenged by Amazon, and Amazon search
is disabled without `KEEPA_API_KEY` — but plain curl succeeds:

```bash
curl -sL --compressed --max-time 30 \
  -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36" \
  -H "Accept-Language: en-CA,en;q=0.9" \
  "https://www.amazon.ca/dp/<ASIN>"
```

- Same ASIN resolves on both `amazon.com` and `amazon.ca` — fetch both as a
  cross-check, and as a fallback if one region challenges you.
- A healthy page is ~2 MB; < 50 KB or a captcha/robot-check block in the head
  means you got challenged — treat as "could not read", not "no price".

### Parse patterns (reliability order)

0. Cart-form hidden inputs: `items[0.base][customerVisiblePrice][amount]`, `[currencyCode]`, `[displayString]` — an unambiguous amount + currency pair right in the buy-box data. Best cross-check when display strings are bare `$134.99` with no currency symbol nearby.
1. Embedded JSON: `"priceAmount":([\d.]+),\s*"currencySymbol":"(\w+)"` — best signal; gives amount + symbol unambiguously.
2. `class="a-offscreen">$X` within the first ~6000 chars of `id="corePriceDisplay..."` (the buy-box price div).
3. Title: `<span id="productTitle">`; stock: `id="availability"`; list price / deal badges: search for `List Price` / `dealBadge` / `Lightning Deal` near the price region.

### Pitfalls

- **Currency is not implied by the domain.** Amazon pages on this host have returned CAD prices for `.com` URLs depending on locale/geo. Always report the currency exactly as the page says it.
- "List Price" above the current price means a standing markdown; a lightning-deal badge means the deal is time-boxed. When a user cites a lower price seen elsewhere (e.g. Reddit), compare it against the *current* parsed price and classify what kind of event that price implies (flash sale vs clearance vs typo).
- Off-screen price arrays (`a-offscreen` across the whole page) include related products and price-breakdown fragments — only trust the ones inside `corePriceDisplay` or the JSON `priceAmount`.
- **Stock markers and price JSON can disagree.** A healthy `/dp` page may contain "In Stock" text (even Add to Cart buttons) while `"priceAmount"` JSON is entirely absent. Report "price could not be read" in that case — do not conclude out-of-stock from missing price JSON, and do not trust the stock text as a price proxy.

### 1b. Direct search (ASIN discovery without a URL)

The engine's Amazon *search* is disabled without `KEEPA_API_KEY`, but a plain `curl` search works from this host — use it to find ASINs when the user describes a product without giving a URL:

```bash
curl -sL --compressed --max-time 30 \
  -A "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36" \
  "https://www.amazon.com/s?k=<url-encoded query>"
```

Parsing: split the HTML on `data-asin="([A-Z0-9]{10})"`; in each following block (~12–15 KB), the title is the `h2` with an `aria-label` and the price is among the first `class="a-offscreen"` spans. Search results prefix prices with an explicit currency (e.g. `CAD 109.61`, `List: CAD 165.09`), so currency is unambiguous there. Sponsored results are labeled in the title; `List:`/`Typical:` lines mark standing or typical prices. A healthy search page is ~1 MB; a challenged one is small or captcha-blocked. Always verify the winner on its `/dp/<ASIN>` page per section 1 — search pages can carry stale or missing prices.

## 2. Silent price watchdog (cron, no LLM)

When the engine can't track a URL and the user still wants an alert, build a
**no_agent cron job**: a script that runs on schedule, prints an alert line
only when the condition is met, and prints NOTHING otherwise. Empty stdout =
no message to the user (the watchdog pattern); a non-zero exit still alerts, so
don't crash on a challenged page — exit 0 silently instead.

Steps:

1. Copy `templates/amazon_price_watchdog.py` to `$HERMES_HOME/scripts/` (`/opt/data/scripts/`), set `ASIN`, `THRESHOLD`, and `URLS`.
2. Test the **silent path** for real: run the script, expect exit 0 and no output.
3. Test the **alert path**: simulate a low price (e.g. string-replace the `priceAmount` in a fetched page and run the parser against it). If the user declines the verification step, explicitly say the alert branch is unverified — never claim the job is proven.
4. Create the cron job: `cronjob action=create`, `no_agent=true`, `script=<path>`, `schedule='30m'` (or the interval the user names). Leave `prompt` empty (ignored in no_agent mode).
5. Set the threshold the user named; if they were vague, propose a concrete one and confirm before creating the job.
6. Verify it stuck: `cronjob action=list` must show the job with a next-run
   time. Jobs are executed by the gateway service's cron ticker (`docker
   compose up -d gateway` on the host runs it); if the list shows the job but
   `cronjob action=status`-style checks say the scheduler is not running, tell
   the user the job will not fire until the gateway container is up.

### Delivery caveat

Cron output from a CLI session is local-only — the user sees it on their next
interaction, not as a push. If they want instant push, a gateway channel
(Discord/Signal/WhatsApp) must be connected; check `check_notifications` and
say plainly that none are configured if that's the state.

## 3. Generalizing beyond Amazon

The pattern generalizes to any store the engine can't reach: curl with a real
browser UA, parse the price out of the HTML (JSON-LD `offers` blocks are the
first place to look on Shopify/custom storefronts), and wrap in the same
no_agent watchdog. Keep one script per item so a parse failure on one item
does not take down a shared job.

### When curl itself is blocked

Some sites reject plain curl entirely (AliExpress returns a 20 KB JS shell,
Reddit returns 403 on its JSON API, eBay returns 403). Do not loop on curl
variants. For **Reddit specifically, stop — use the engine's `community_pulse`
and `read_reddit_thread` tools**, which ride the RSS feeds Reddit serves
freely. For other blocked sites, either (a) delegate the verification to a
subagent with browser-based tools, or (b) report honestly "could not read,
source blocks non-browser access" and proceed with the prices you *did*
verify. A verified price from one store is always worth more than a guessed
price from another.

### When description-based tracking finds no match

`track_product_description` / `compare_prices` now retry an over-detailed
description with its compact core (brand + model) automatically — the
response's `search_terms_used` shows what was actually searched. So a zero-hit
result means the catalog stores genuinely don't list it under that name: do
not burn turns rewording the same query. Instead, verify the price on a
specific listing via the direct-fetch patterns in section 1, then use
`track_product_url` on that URL.
