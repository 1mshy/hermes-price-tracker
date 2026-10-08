# Hermes Shopping

A price-tracking shopping assistant: **NousResearch [Hermes Agent](https://hermes-agent.nousresearch.com)**
in Docker, talking to your own model, wired over MCP to a price engine that reads
live prices from tech and 3D-printing retailers and pings you on Discord, Signal
or WhatsApp when something gets cheap.

```
┌────────────┐   MCP over HTTP    ┌─────────────────────────────┐
│  hermes    │ ─────────────────► │  pricewatch                 │
│  (agent)   │                    │  • 48 store adapters        │
└─────┬──────┘                    │  • price history (SQLite)   │
      │ OpenAI API                │  • scheduled sweeps         │
      ▼                           │  • alert rules              │
 http://10.150.0.30:1234/v1       └──────────┬──────────────────┘
 (vLLM · Qwen3.8-27B)                        │
                                   ntfy / Discord / Signal / WhatsApp
```

## Quick start

```bash
cp .env.example .env          # then edit: model endpoint, dashboard password, channels
docker compose up -d
docker compose exec hermes hermes       # interactive agent in the terminal
```

Two containers: `pricewatch` is the price engine, `hermes` is the agent. The
agent container runs the gateway, the cron ticker and the browser dashboard
together under s6 supervision — that is one container by design, not an
accident. Hermes' agent home is a single-writer store, so a second container
pointed at the same volume corrupts sessions and memory. You reach the terminal
agent with `exec`, not by starting another one.

Then open **http://localhost:9119** and sign in.

Then just talk to it:

> *"What does a Bambu Lab P1S Combo cost right now?"*
> *"Track https://store.creality.com/products/k1-se-3d-printer and tell me if it drops 12%."*
> *"Watch for Polymaker PolyTerra PLA 1kg matte black under $18 anywhere."*
> *"Reddit says people are getting the SUNLU AMS heater for $50 — is that real?"*
> *"Check my tracked prices every 15 minutes instead."*
> *"What am I tracking, and has anything moved?"*

## Add products without a conversation

Open **[your watchlist](http://localhost:8077/watchlist)**. Paste a product link
or type a product name; add several products by putting one on each line. Saving
is immediate and does not need the model server. Hermes looks up prices in the
background, keeps the watch across restarts, and retries unavailable retailers
on later sweeps. Repeated submissions reuse an existing watch and keep its rules.

The default alert is a **5% drop** from the first verified price. You can set a
target price or restock alert before adding, or edit the rule later. The page
shows pending, watching, retrying and paused items, listing links, currencies,
check times, and last known prices. Pause and resume items there too. A target
on a URL uses its actual listing currency; a name search uses your preferred
currency. Failed or sold-out listings cannot trigger a price-deal alert.

In chat, “watch this” uses the same `add_watch` tool and saves immediately; it
does not require a target-price interview. `POST /api/trackers` accepts
`{"input":"product URL or name"}` with optional `target_price`, `drop_pct` and
`alert_on_restock` fields. The original URL/query API endpoints remain available.

## Quiet, useful background work

Routine checks, unchanged prices, empty results and transient failures stay
local. The morning brief and community sweep send a push only for a new,
actionable buying opportunity. Their scheduler delivery is local, so a failed
model connection no longer produces an additional channel message. Container
startup upgrades the known shopping jobs while preserving schedules and custom
product instructions.

Price checks and new-product lookups run independently of the model. Unresolved
name searches retry on each sweep; healthy name watches search for more listings
daily. Watches whose listings repeatedly fail also try alternative retailers,
at most daily. A failed read retains its history but cannot trigger a deal alert.

The health watchdog first tries to restore a missed sweep using the existing
schedule. A persistent incident is reported once after two observations, and
does not repeat until it has recovered and failed again. A blocked alternative
retailer is not reported when another listing still covers the watch. Local
diagnostics are saved in `$HERMES_HOME/cron/health_watchdog_status.json`.

## Browser dashboard

Hermes ships its own web UI — chat with the agent, browse past sessions, and edit
model/provider/API-key config without touching a file. It comes up with the
`hermes` container on **http://localhost:9119**, wired to the same config and the
same `pricewatch` tools as the terminal agent, so you can do all the price work
from the browser.

It binds to `0.0.0.0` inside the container so you can reach it from your machine,
and Hermes refuses to do that without an auth provider. Set a password in `.env`:

```ini
HERMES_DASHBOARD_BASIC_AUTH_USERNAME=admin
HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=<choose one>
HERMES_DASHBOARD_BASIC_AUTH_SECRET=<random string>   # keeps you signed in across restarts
```

Without one Hermes fails closed and the dashboard refuses to serve; the
container logs a warning at boot naming the two variables to set. The gateway and
the cron ticker keep running regardless — a dashboard misconfiguration should not
take scheduled jobs down with it.

**Theme:** the dashboard ships eight built-in skins, and Hermes also scans
`$HERMES_HOME/dashboard-themes/*.yaml` for user themes. `hermes/dashboard-themes/`
holds ours — the vendored [boring-dark / boring-light](https://github.com/sorenisanerd/hermes-dashboard-themes)
pair (flat, no teal tint, no grain, system fonts) — and the container's init script
copies them into the agent home on every start, so they show up in the switcher next
to the built-ins. Pick the active one in `.env`:

```ini
HERMES_DASHBOARD_THEME=boring-dark   # or boring-light, default, midnight, ember, mono, cyberpunk, rose
```

That is written to `dashboard.theme` in the generated `config.yaml`, which is also
where the UI's own **Switch theme** menu saves — so switching in the browser works
but is reset on the next `docker compose restart hermes`. Change `.env` for a
durable choice. To add another theme, drop its YAML in `hermes/dashboard-themes/`
and rebuild (`./scripts/rebuild.sh agent`); the directory is baked into the image,
so a rebuild is what installs it.

**Exposing it beyond localhost:** the published port is bound on your machine. If
you want it reachable from elsewhere, put it behind a tunnel or reverse proxy with
TLS rather than opening port 9119 — the session cookie is only as safe as the
transport.

Useful commands:

```bash
docker compose logs -f hermes         # startup + request log
docker compose restart hermes         # pick up .env changes
```

## What actually works

Verified live against every store in the catalog (`docker compose exec pricewatch
python -m pricewatch.verify`). Latest full run (2026-09-19 UTC): **37 of 48 stores returned a product price**, up from 33. Seven shops returned access challenges, Best Buy US needs a key, AliExpress and LDO have explicit tracking limitations, and Gulf Coast's hostname did not resolve. Repeated Amazon and Walmart checks, forced Chromium tests and real browser crash/session recovery are documented in `reports/2026-09-19-retailer-reliability.md`. These are point-in-time observations from this Docker host, not a promise that every product remains readable.

**Available extraction routes (live access varies by retailer):**

| | |
|---|---|
| **Shopify JSON** (exact price, in-stock, variants) | Elegoo · Anycubic · Printed Solid · E3D · Slice Engineering · Micro Swiss · West3D · Fabreeko · Filastruder · QIDI · Sovol · Polymaker · Proto-pasta · Atomic Filament · Overture · Fillamentum · SUNLU · **Voxel Factory · 3D Printing Canada · Filaments.ca · DigitMakers · Shop3D.ca** (all CAD) · Creality's `ca.` market |
| **schema.org JSON-LD over fingerprinted HTTP** | Bambu Lab · Prusa · Creality · B&H Photo · DigiKey (last two blocked in the latest live run) |
| **Keyless retailer JSON API** (bestbuy.ca is a different platform from bestbuy.com) | Best Buy Canada — exact price, stock, sale end date, third-party sellers flagged `marketplace` |
| **schema.org JSON-LD over plain HTTP** (PrestaShop / BigCommerce, CAD) | Canada Computers (product pages + parsed search cards) · Spool3D |
| **Fingerprinted HTTP + DOM/buy-box** | Amazon · Walmart · Newegg · 3DJake · Memory Express (CAD) |
| **Headless browser + JSON-LD** | MatterHackers · TH3D · Target (DOM) |

**The fast path is a browser TLS fingerprint.** Retail bot walls (Cloudflare,
Akamai, Amazon) fingerprint the TLS handshake and HTTP/2 settings, not the header
set — so plain `httpx` gets challenged even with perfect headers. The engine
sends a real Chrome fingerprint (via `curl_cffi`, `PW_IMPERSONATE`), which clears
most walls in ~1s with no browser. **Amazon, Walmart and Newegg returned prices without a key or proxy in
the latest run.** B&H and DigiKey, which worked previously, currently return
access challenges from this host. A headless browser also retries challenged pages and pages whose price
only appears after JavaScript runs.

**Marketplaces** — eBay supports a keyless adapter (fingerprinted HTTP + JSON-LD
on item pages, HTML parsing for search), but the latest verifier could not
discover a readable product URL; `EBAY_APP_ID` + `EBAY_CERT_ID` remain the
most reliable path. AliExpress is **search-only**: `compare_prices` returns live
prices from its search page (often the cheapest source — OEMs like SUNLU sell
direct), in whichever currency `PW_PREFERRED_CURRENCY` pins the locale to, but
product pages block automated reads, so AliExpress listings cannot be tracked
and the tools say so instead of failing silently.

**Retailer recovery and fallbacks** — SUNLU uses its actual Shopify store at
`store.sunlu.com`, including recovery for renamed product handles. KB-3D product
discovery starts under `/store/`. Memory Express uses its main CAD price
container. Target waits for its client-rendered product price. Configurator
ranges and unrelated recommendation prices are not treated as exact quotes.

**Official and managed APIs** — Best Buy US requires `BESTBUY_API_KEY`; eBay
supports approved `EBAY_APP_ID` / `EBAY_CERT_ID` credentials. Amazon first reads
the product page, with Keepa as an optional paid fallback. Keepa requests a
refresh, checks product identity and observation age, and reports its price
basis. No third-party service makes storefront availability unconditional.

An additional **opt-in SerpApi product fallback** supports Amazon's configured
markets and **walmart.com only**. Set `SERPAPI_KEY` and
`PW_RETAIL_SERPAPI_ENABLED=true` to enable it. It runs only after direct access
fails, requests uncached data, validates the item and timestamp, and uses one
provider request per fallback. It may consume paid credits. Walmart Canada is
never substituted with a US quote. Provider integrations have contract tests;
without configured credentials their live operation is unverified.

**Access restrictions** — eBay, B&H, Micro Center, Adorama, Siboor, DigiKey and
Mouser have returned challenges through both HTTP and Chromium on this host.
`PW_HTTP_PROXY` applies to both transports, including proxy authentication in
Chromium. A suitable proxy or approved API can provide another route, but access
must be tested with that service. DigiKey and Mouser have official product APIs;
those two integrations are not implemented here. LDO's site is a manufacturer
catalog; use its resellers for retail quotes. Gulf Coast's domain did not resolve
in the latest investigation.

The engine remembers successful transport choices for one day and unsuccessful
browser attempts for five minutes, then probes again. Missing products,
rate-limit/server errors and a disabled browser do not permanently block a
store. Chromium runs at most two navigations at once, retains up to eight
storefront contexts, saves dedicated anonymous sessions under
`data/browser_sessions/`, and relaunches after losing its browser connection.
`PW_BROWSER_STATE_DIR=off` disables session persistence. Settings are documented
in `.env.example`; there is no access to your personal browser profile.

Read the retailer investigation in `reports/2026-09-19-retailer-reliability.md`.
The repeatable smoke runner `scripts/retailer-smoke.py` checks multiple products,
forces browser fallbacks and verifies session/crash recovery. It disables paid
APIs and uses temporary state; it does not create watches or send notifications.

`list_stores` (or `GET /api/stores`) reports each store's method and limitation,
so the agent can explain gaps instead of quietly dropping a retailer.

## How prices are read

Preference order, per store — cheapest and most reliable first:

1. **Official API** — Best Buy and eBay when credentials are configured; Best Buy Canada uses its public product endpoint.
2. **Platform JSON** — Shopify `/products/<handle>.js` (falling back to the
   older `.json` view on themes that gate it), WooCommerce Store API.
   Public storefront endpoints, exact prices in minor units, no HTML parsing.
3. **Fingerprinted HTTP** — a GET with a real Chrome TLS/HTTP2 fingerprint
   (`curl_cffi`), then schema.org JSON-LD / microdata / OpenGraph, Amazon's
   buy-box JSON, or site DOM selectors. This is the workhorse for the big-box
   retailers and clears most bot walls without a browser.
4. **Headless Chromium** — only when the price is rendered client-side or a wall
   fingerprints deeper than TLS.
5. **Optional fallback providers** — fresh Keepa data for Amazon, or explicitly
   enabled SerpApi product data for Amazon and Walmart US. `PW_HTTP_PROXY`
   routes both HTTP and Chromium when configured.

The cheapest rung that works for each host is cached in a per-host playbook, so
subsequent reads start there. Requests are rate-limited per hostname
(`PW_PER_HOST_RPS`, default 0.4/s) with jitter. Unknown domains are sniffed
automatically — paste a link to any Shopify or WooCommerce store and it will
usually just work. Shopify is recognised from an open `/products.json` or, where
a store gates that, from the `cdn.shopify.com` fingerprint its own theme leaks.

## Currency and region

Set the currency you want to be quoted in, and the market you buy from:

```bash
PW_PREFERRED_CURRENCY=CAD    # ISO code: CAD, EUR, GBP, AUD, JPY, CHF, SEK, PLN, CZK, USD
PW_REGION=CA                 # ISO country; blank derives it from the currency
```

Or set it in conversation — "quote me in CAD from now on" — which the agent
applies through `set_preferred_currency` (`PATCH /api/locale`). A runtime change
is stored in the settings table and survives restarts, exactly like the sweep
schedule.

What a preference actually changes:

- **Search goes to your storefront.** Where a store runs a regional site the
  engine searches that one — `ca.store.bambulab.com` instead of
  `us.store.bambulab.com` — so the prices come back natively in your currency
  rather than needing conversion. AliExpress's locale cookie is pinned to the
  same market, so its search returns CAD directly.
- **Shopify stores report the right money.** Shopify's product JSON gives cents
  and no currency at all, so the engine asks the storefront itself — one cached
  `/cart.js` read per market — and only falls back to the domain (`ca.…` → CAD)
  when that fails. A Montreal shop on a `.com` was otherwise quoted in USD.
- **Shopify Markets is a price list, not a translation.** One storefront serves
  several markets behind locale prefixes, each with its own currency *and its
  own prices*: `shop.polymaker.com` quotes US$25.99 for the spool `/en-ca/`
  quotes at CA$36.99. Search moves to the market that bills in your currency,
  so those prices are native rather than converted. A URL you paste keeps
  whatever market it names — the prefix is preserved into every endpoint, and
  tracking a `/en-ca/` link keeps following the Canadian price.
- **Foreign listings are labelled, not converted.** A store that bills in
  another currency keeps its own figure. Beside it the result carries
  `approx_in_preferred` — an indicative conversion the agent is instructed to
  show as an approximation (`$99 USD ≈ CA$136`), never as the price, and never
  as an alert threshold.
- **The agent is told.** The preference is part of the MCP server's
  instructions, so it applies whether or not the price-tracking skill has
  loaded; `get_preferred_currency` and `engine_status` report the live value.

Ordering across mixed currencies already used an indicative rate so a CA$96
listing does not outrank a US$99 one. That rate is now the ECB reference
rate, fetched keylessly from [Frankfurter](https://frankfurter.dev) every six
hours (`PW_FX_REFRESH_CRON`) and cached in the settings table across
restarts; when the provider is unreachable, or `PW_FX_ENABLED=false`, a static
table in `fx.py` stands in. Every `approx_in_preferred` hint says which one it
used (`rate_source`, `rate_as_of`), `get_exchange_rates` / `GET /api/fx`
report the table in force and how old it is, and nothing about the rule has
changed: rates are for ranking and orientation only, never for quoting.

**Canadian storefronts.** For a CAD shopper the catalog is no longer a list of
American shops with a conversion beside each price. The engine searches the
OEMs' own `ca.` markets natively — `ca.store.creality.com`, `ca.elegoo.com`,
`ca.anycubic.com`, `ca.qidi3d.com` — reads `ca.store.bambulab.com` product
pages (Bambu Lab publishes no search), and searches the Canadian retailers
that actually stock 3D-printing and tech gear: Voxel Factory (Montreal), 3D
Printing Canada, Filaments.ca, DigitMakers, Shop3D.ca, Spool3D, Best Buy
Canada (keyless JSON API; `marketplace` listings are third-party sellers, and
a watch created from a description never baselines on one, the same way it
skips refurbs) and Canada Computers, plus walmart.ca and newegg.ca product
pages. A bare `$` on a `.ca` page is read as CAD, not USD. Every store carries
a `country` — the market its primary storefront sells in — in `list_stores` /
`GET /api/stores`, and `compare_prices` / `POST /api/compare` accept
`country: "CA"` as the shortcut for "only the stores that sell in my market".

## Alerts

A watch fires when any of its conditions is met (it needs at least one):

- `target_price` — absolute, e.g. alert under $250.
- `drop_pct` — relative to the price when the watch was created.
- `alert_on_restock` — a sold-out listing on the watch comes back.

Both are in the **watch's currency** — that of the listing the watch was created
from (the user's own whenever a match bills in it). Only listings in that
currency are measured against them; a listing in another currency stays on the
watch for reference, and `list_trackers` counts it under `foreign_listings`, but
it never fires the alert. USD 99.99 is not "16% below" a CA$119 baseline, and
amazon.com shows a Canadian visitor a *converted* figure that reads CAD on one
fetch and USD on the next — which is why a pasted Amazon link is registered on
the regional marketplace (amazon.ca) whenever the ASIN resolves there.

**Coupons count.** Amazon's clip coupons sit beside the buy box — the sticker
stays put and a checkbox takes the money off at checkout — so a watch that
only read the sticker sat through a CA$167.73 → CA$49.99 coupon on the Sunlu
AMS heater (September 2026) without a flicker. The Amazon adapter now reads
the coupon widget and the deal badge: with a coupon on the page, the price the
tracker measures is the after-coupon figure, `extra.sticker_price` keeps the
sticker, and a `deal_note` ("CA$49.99 after the on-page coupon … clip it before
checkout") is stored on the listing, shown by `list_trackers`, and printed in
the alert. Limited-time deals are annotated the same way (`extra.deal`,
`extra.list_price`). Coupons are time-boxed and can be account-specific: what
the engine sees from its own address is what it reports. The same change also
fixed deal pages reporting the struck-through *list* price instead of the sale
price, which newer Amazon markup made possible.

Alerts are deduplicated by a per-tracker cooldown (default 12 h) that is bypassed
only when the price falls *further* than the last alert; once it expires, a
repeat goes out only if the price has moved since the last one, so a slow slide
keeps notifying while a flat price stays quiet.

Restock alerts are events, not levels, so none of that applies to them. One
fires when a listing in the watch's currency goes from out of stock to in stock
(a first sighting is not a restock), names the cheapest such store and its
price, and adds a line if that listing also meets the price condition. There is
no cooldown and no repeat until the listing has sold out and come back again. A
sweep that cannot read a listing leaves its last known stock state alone, so
one blocked read between "sold out" and "back" does not lose the restock.
Stock changes are written to the price history alongside price changes
(without counting as price observations), `list_trackers` reports
`out_of_stock_listings` per watch, and a watch created on a sold-out listing
without `alert_on_restock` says so in its `note`.

Configure channels in `.env`:

- **ntfy push** — `NTFY_TOPIC`. The fastest path to real push: no account,
  just pick an unguessable topic and subscribe to it in the
  [ntfy app](https://ntfy.sh). Treat the topic name like a password.
- **Discord** — `DISCORD_WEBHOOK_URL`. Easiest webhook; no bot needed.
- **Signal** — `docker compose --profile signal up -d signal-cli`, register the
  number, then set `SIGNAL_FROM` / `SIGNAL_TO`.
- **WhatsApp** — Twilio (`TWILIO_*`) or CallMeBot (`CALLMEBOT_*`, no account).

Check delivery end to end: `curl -XPOST localhost:8077/api/notify/test`, or ask
the agent *"can you send me a test alert?"*.

Every fired alert is also recorded whether or not a channel delivered it —
`list_alert_events` (or `GET /api/alerts`) is the audit trail, which matters
when no channel is configured yet and alerts would otherwise be invisible.
The agent's own pushes go through the same door: the `notify_user` tool
(`POST /api/notify`) sends through every configured channel and, given a
`tracker_id`, lands in the same trail as kind `agent` — so "what did you
already tell me, and when?" has one answer.

The sweep cadence itself is live-adjustable: the agent's `set_sweep_schedule`
tool (or `PATCH /api/schedule`) takes a 5-field UTC cron expression, refuses
anything more frequent than every 5 minutes, persists across restarts, and
`GET /api/schedule` shows the active schedule with the next sweep time.

## Community intel (Reddit)

Deal chatter usually precedes the price move — "$50 with the checkout coupon"
threads are how the good deals actually surface. The engine reads Reddit over
its public RSS feeds (the JSON API blocks datacenter clients; RSS is served
freely) with the same per-host politeness throttle as everything else:

- `community_pulse` — recent posts across the deal subreddits (3Dprinting,
  BambuLab, 3dbargains, buildapcsales by default), newest first, with any
  prices mentioned in the text extracted per post.
- `read_reddit_thread` — one thread with its top comments: coupon code,
  region, expiry, whether the deal died.

Both are also on the REST API (`GET /api/community?query=…`,
`GET /api/community/thread?url=…`). Community prices are unverified leads by
design — the tools say so in their output, and the skill tells the agent to
confirm with `get_price` before quoting one.

## Used-car search (the swarm)

Cars are not products. There is no catalogue, no SKU to match on, and the same
car is cross-posted to four sites at three prices; "cheapest" is close to
meaningless when the cheap one is the one with 99,000 km on it. So this is a
separate engine from the price tracker, with its own sources and its own idea
of what an answer looks like.

```
   "all the 2022 audi a3s in laval, quebec"
                  │
        ┌─────────┴──────────┐   five scouts, in parallel
        ▼                    ▼
  AutoTrader.ca  Kijiji  Carpages  Marketplace  Copart
        └─────────┬──────────┘
                  ▼
     place · filter · deduplicate · fit the local price curve
                  ▼
       report  ◄── analyst agents (your own model)
```

Ask the agent directly:

> *"What 2022 Audi A3s are for sale near Laval?"*
> *"Find me a manual Golf GTI around Montreal under $20k with less than 120,000 km."*

**Sources.** `autotrader` and `carpages` are dealer inventory read from the
schema.org JSON-LD both publish. `kijiji` is the richest — its Apollo cache
carries VIN, trim, odometer, seller type, coordinates and Kijiji's own price
rating. `facebook` is Marketplace, recovered from the GraphQL payload the page
ships inline, and is where the private sellers are. `copart` is the salvage
auction, included because it is the floor under every asking price in the
report — its lots are marked `salvage`/`auction` and never mixed into the
retail statistics.

**Nothing is trusted to filter itself.** A Laval search on AutoTrader returns
Winnipeg cars; Kijiji hands back a Q3 when you asked for an A3; Copart matches
on the make alone. So every listing is re-placed on the map (cities are
geocoded and cached) and re-checked against the query locally. In a typical run
167 of the 182 listings read are discarded, and the report says so.

**Cross-postings are merged.** One car on AutoTrader, Kijiji and Marketplace is
one car, or the market looks three times bigger than it is. Records are joined
on VIN where a source publishes one and on year/model/price/odometer where none
does, with an ambiguity check that leaves two same-priced cars apart rather than
guessing. Every merged record keeps the other URLs.

**"Best price" is the wrong question, so it is not the answer.** The engine fits
asking price against odometer across the cars actually for sale near you, which
gives an expected price at any mileage and turns "is this a deal" into a number.
It then reports the **Pareto frontier** — the cars nothing else beats on both
price *and* kilometres. Everything else is strictly worse than one of those on
both counts, so no preference between money and mileage could pick it.

**Your model writes, it does not count.** Prices, distances and deal scores all
come from the arithmetic; the model reads the request, reads what the sellers
wrote (most of this market advertises in French), and writes the prose. Point it
at any OpenAI-compatible endpoint — it defaults to the same `LLM_BASE_URL` /
`LLM_MODEL` the agent already uses, and the whole report degrades to the
deterministic version when the endpoint is absent or slow.

```bash
curl -XPOST localhost:8077/api/cars/search -H 'Content-Type: application/json' \
     -d '{"make":"Audi","model":"A3","location":"Laval, Quebec","year":2022}'
curl -XPOST localhost:8077/api/cars/search -H 'Content-Type: application/json' \
     -d '{"text":"2019-2022 Honda Civic near Toronto under $25,000","deep":true}'
curl localhost:8077/api/cars/sources
```

`deep=true` adds the reader agents that summarise each seller's own claims and
warnings; it is slower and off by default.

**What it does not do.** It does not crawl individual dealership websites. The
Quebec dealer platforms render their inventory client-side and publish no
vehicle JSON-LD, no per-car URL and no VIN — while those same cars are on
AutoTrader and Kijiji *with* all three. So the dealer view is built from the
aggregator records instead, and every figure in it has a live listing behind it.
Facebook Marketplace is read logged-out, which returns roughly the first screen
of results and no pagination: treat it as a sample of the private market, not a
census.

## Scheduled agent jobs (hermes cron)

Price watches don't need cron — trackers re-check themselves on the sweep
schedule. Hermes cron is for what the engine can't do alone: a morning Reddit
deals briefing, or a silent no-LLM watchdog script for a store the engine
reports as blocked (see `hermes/skills/price-tracking/price-watch-fallback/`,
which also ships `templates/pricewatch_health_watchdog.py` — a silent 6-hourly
job that attempts recovery and speaks up once per persistent incident: the
engine is down, sweeps remain stalled, an entire watch has no working listing,
the model endpoint is unavailable, or a research job repeatedly fails. It
confirms the incident across two observations and pushes through the engine's
channels, or directly to ntfy when the engine is down. Model availability and
price monitoring are reported separately).

Schedule syntax matters: `"6h"` means **once**, in six hours. For recurring
jobs use a cron expression (`"0 */6 * * *"`) — `hermes cron list` shows
`Repeat: ∞` when you got it right.

Two pieces make agent-created cron jobs actually work, and both are wired into
`docker-compose.yml`:

1. **`HERMES_INTERACTIVE=1`** — the agent's `cronjob` tool is gated on an
   interactive-capable session and never loads without it.
2. **`command: gateway run`** — hermes' cron ticker only runs inside a gateway
   process (`hermes cron status` says exactly this). It is the container's main
   program, so jobs created from the dashboard or the terminal fire in the same
   container that holds the agent home.
3. **`HERMES_ACCEPT_HOOKS=1`** — a scheduled job that wants to run a hook script
   would otherwise stall on an approval prompt with nobody there to answer it.
   Note this now applies to dashboard chats too, which the old separate gateway
   container kept it away from.

The prompts of the two LLM-driven jobs (`morning-deals-brief`, the Sunlu
community sweep) are kept in `hermes/cron-prompts/`; jobs themselves live in
the agent-home volume. Rebuild the agent after editing a template. Startup
updates managed templates and applies quiet delivery to the known shopping
jobs, preserving schedules, disabled status and user-customized prompts.

Useful commands:

```bash
docker compose exec hermes hermes cron list     # what's scheduled, next runs
docker compose exec hermes hermes cron runs     # execution history
docker compose exec hermes hermes cron status   # is the ticker alive
```

Delivery: shopping jobs use `deliver=local`, so routine output and scheduler
errors remain in their run history. Their prompts call `notify_user` only for
new actionable deals after checking the alert trail for duplicates. This
avoids sending both the tool notification and the same final response. The
ntfy *platform* (the agent listening on a
topic, `--deliver ntfy`) is opt-in via `HERMES_NTFY_INBOUND_TOPIC`: it must be
a different topic from the alerts one, or the agent reads its own alerts as
user messages.

## Model configuration

`hermes/cont-init.d/018-hermes-shopping` renders `/opt/data/config.yaml` from
`.env` on every start, so the container stays declarative — change `.env`,
restart, done.

Three settings matter for a self-hosted endpoint, and all three are load-bearing:

| Setting | Why |
|---|---|
| `LLM_CONTEXT_LENGTH=131072` | Hermes assumes a 256K window when it cannot detect one, and refuses to run below 64K. Set your server's real `max_model_len` (`curl $LLM_BASE_URL/models` reports it). |
| `LLM_MAX_TOKENS=8192` | Left alone, Hermes requests `max_tokens == context_length`; vLLM requires `prompt + max_tokens <= max_model_len`, so every call would fail once and retry. |
| `_config_version: 39` (in the generated config) | Without it Hermes reads the file as v0, below its v12 auto-migration floor, and every load prints "config predates version 12". Bump it with the base image tag; `hermes doctor` reports the version it wants. |

## REST API

The agent's capabilities are also plain HTTP on `localhost:8077`:

```bash
curl "localhost:8077/api/price?url=https://us.elegoo.com/products/..."
curl -XPOST localhost:8077/api/compare -H 'Content-Type: application/json' \
     -d '{"query":"Polymaker PolyTerra PLA 1kg"}'
curl -XPOST localhost:8077/api/trackers/url -H 'Content-Type: application/json' \
     -d '{"url":"https://...","drop_pct":12}'
curl -XPOST localhost:8077/api/trackers/url -H 'Content-Type: application/json' \
     -d '{"url":"https://...","alert_on_restock":true}'
curl localhost:8077/api/trackers
curl -XPOST localhost:8077/api/refresh
curl localhost:8077/api/alerts
curl "localhost:8077/api/community?query=sunlu+ams+heater"
curl localhost:8077/api/schedule
curl -XPATCH localhost:8077/api/schedule -H 'Content-Type: application/json' \
     -d '{"cron":"*/15 * * * *"}'
curl localhost:8077/api/locale
curl -XPATCH localhost:8077/api/locale -H 'Content-Type: application/json' \
     -d '{"currency":"CAD"}'
curl localhost:8077/api/fx                       # exchange rates in force, source and age
curl -XPOST localhost:8077/api/fx/refresh
```

Interactive docs at `localhost:8077/docs`.

## Verifying stores

```bash
docker compose exec pricewatch python -m pricewatch.verify              # all stores
docker compose exec pricewatch python -m pricewatch.verify --tag 3dprinting
docker compose exec pricewatch python -m pricewatch.verify --store prusa --store bambulab
docker compose exec pricewatch python -m pricewatch.verify --json /data/report.json
```

Product URLs go stale constantly, so the harness **discovers a live product per
store** at run time (Shopify catalog → sitemap → homepage scrape) rather than
trusting hardcoded links, and reports `OK` / `BLOCKED` / `NEEDS-KEY` / `FAIL`
separately so a bot-wall is never confused with a broken parser.

## Rebuilding after a change

Source is baked into the images (`COPY src` + `pip install .`), so `docker
compose restart` keeps running the *old* code — a stale pricewatch image once
went unnoticed for days. `scripts/rebuild.sh` is the safe path: it runs the
offline suite, rebuilds, waits for the healthcheck, then **diffs the installed
package against your working tree** and fails loudly if they differ.

```bash
./scripts/rebuild.sh                 # pricewatch (the usual case)
./scripts/rebuild.sh agent           # the hermes container (hermes/ changes)
./scripts/rebuild.sh all             # pricewatch + hermes (+ signal-cli if enabled)
./scripts/rebuild.sh all --force     # all-in-one: rebuild both images, restart every container
./scripts/rebuild.sh --force         # restart the targets even if nothing changed
./scripts/rebuild.sh --smoke         # also run scripts/agent-smoke.sh
./scripts/rebuild.sh --dry-run       # show what compose would do; no build, no restart
```

After a pricewatch rebuild the script also recreates `hermes`, whose open
dashboard session otherwise holds an MCP stream to the container that just went
away. `docker compose up` only recreates a container whose image or config
changed, so a plain run leaves an untouched container alone; `--force` restarts
every target regardless, which is the one command to use when you want the
whole stack rebuilt and restarted. `agent` builds with `--no-deps` on purpose:
compose otherwise rebuilds pricewatch as a dependency of hermes, which would
deploy untested engine code with no drift check. The hermes image gets the same
after-build check as pricewatch — skills, dashboard themes and the init hook are
hashed inside the container and diffed against `hermes/`.

### Upgrading the agent

The agent image is the official `nousresearch/hermes-agent`, pinned to a date
tag in `hermes/Dockerfile`. Upstream retired the PyPI install path — `pip install
hermes-agent` is now listed as unsupported alongside Homebrew and the AUR, "may
already be broken right now", and the dashboard nags about it on every load.
Docker is the Tier 1 target, so this repo layers its skills, themes and generated
config onto that image rather than building its own.

A Docker install has no `hermes update`. Upgrading is:

```bash
# 1. pick a tag: https://hub.docker.com/r/nousresearch/hermes-agent/tags
# 2. edit the FROM line in hermes/Dockerfile
# 3. back up the agent home first — the volume is the only copy of your
#    cron jobs, sessions and memories
mkdir -p backups && docker run --rm -v hermes-shopping_hermes_home:/from:ro \
  -v "$PWD/backups":/to alpine tar czf /to/hermes_home-$(date +%F).tar.gz -C /from .
./scripts/rebuild.sh agent
docker compose exec hermes hermes doctor      # check the config version it wants
```

`hermes doctor` is the check that matters: if it reports a config version newer
than the `_config_version` written by `hermes/cont-init.d/018-hermes-shopping`,
bump that number too. `latest` is deliberately not used — it moves near-daily.

## Running the tests

The engine has an offline unit suite (price parsing, product matching, HTML
extraction, Reddit feed parsing, alert rules, schedule validation — no network):

```bash
cd pricewatch
uv venv .venv && uv pip install -p .venv/bin/python -e . pytest
.venv/bin/python -m pytest tests/ -q
```

Agent-level smoke (drives the real LLM through the MCP + cron surface):

```bash
./scripts/agent-smoke.sh
```

## Adding a store

Add one row to `pricewatch/src/pricewatch/stores/catalog.py`:

```python
{"key": "mystore", "label": "My Store", "kind": "shopify",
 "tags": [PRINT3D], "domains": ("mystore.com",)},
```

`kind` is `shopify`, `woo`, `structured`, `browser`, `selector`, or `api`. For
`selector`, add XPaths to `SELECTOR_RULES` in `stores/registry.py`. `country`
defaults from the primary domain's TLD — set it explicitly where the TLD lies
(a Montreal shop on a `.com`): a `structured`/`browser` store then reads a
bare `$` as that country's dollar (Shopify stores ask `/cart.js` regardless).
When country and hostname both mislead (Prusa is Czech; its `.com` quotes
USD), set `currency` to what a bare number means there. Regional storefronts
go after the primary in `domains` so `storefront()` can pick them for a
shopper in that market; a structured store whose regional market runs on
Shopify lists it under `shopify_markets`. Then verify it:
`python -m pricewatch.verify --store mystore`.

## Laptop sleep, battery and power

The stack runs on a MacBook that sleeps, gets unplugged and moves between
networks. Docker Desktop freezes the containers with the host and thaws them
on wake; what needs care is the minute after that thaw and the case where
Docker itself is not running. Four pieces cover it:

1. **A launchd user agent on the Mac** — `scripts/host/install-wake-agent.sh`
   installs `com.hermes-shopping.wake`, which runs
   `scripts/host/wake_recover.py` at every even minute. It uses
   `StartCalendarInterval`, which coalesces missed events on wake;
   `StartInterval` drops sleep-time events and is deliberately not used. In
   steady state with everything healthy it does nothing and logs nothing.
   When Docker is down it starts Docker Desktop (on AC power always; on
   battery only with `--start-docker-on-battery`), brings the containers up
   with `docker compose up -d --no-build --wait` (never a build — that is
   `scripts/rebuild.sh`), waits for `/api/health` and for DNS to resolve
   *inside* the engine container, and then — after a gap long enough to have
   been a sleep (or a new kernel wake timestamp) — POSTs `/api/refresh` for one catch-up sweep and re-runs
   once any hermes cron job that failed during the resume window. It holds a
   `caffeinate -i` assertion while it works so a battery-powered Mac does not
   doze off mid-sweep. Failed recovery stays pending for the next host run,
   and unhealthy running containers are restarted before waiting for health. Log: `~/Library/Logs/hermes-shopping/wake.log`.

   ```bash
   ./scripts/host/install-wake-agent.sh              # install/refresh, runs once now
   ./scripts/host/install-wake-agent.sh --status
   ./scripts/host/install-wake-agent.sh --wake-at 08:55   # sudo pmset repeat: daily wake before the 09:00 brief
   python3 scripts/host/wake_recover.py --catch-up -v     # what the agent does after a wake, by hand
   python3 scripts/host/wake_recover.py --dry-run --catch-up
   ```

2. **The engine runs a missed sweep once, when it can.** The scheduler has no
   misfire grace: a slot that passed while the container was frozen fires as
   soon as the loop is back, and any number of missed slots coalesce into that
   one run. Before sweeping it waits (about a minute) for name resolution,
   because Docker's host-side resolver is the last thing to come back after a
   wake. Each DNS probe has a five-second deadline. If DNS is still gone,
   it schedules another attempt sixty seconds later without changing the
   regular cadence. `/api/health` reports `sweep.last_skipped_at`,
   `sweep.recovery_sweep_at`, and `sweep.sweep_running`. A container restart
   also triggers one immediate sweep, since an in-memory scheduler forgets
   missed slots across restarts. Overlapping full sweeps share one task even
   if the first caller disconnects; alert evaluation is serialized to avoid
   duplicate sends from overlapping targeted and scheduled refreshes.

3. **Alerts are not consumed by a failed push.** Notification channels retry
   transport failures (DNS, connect, timeout), HTTP 429, and temporary server
   errors up to three times, with 2/5/15-second backoffs in addition to request
   timeouts. `Retry-After` is honored; delays over thirty seconds are deferred
   to a later sweep, with the deadline retained for the current process. Authentication and invalid-payload errors fail immediately. If every configured channel still
   fails, the watch's cooldown is *not* started: the alert is recorded as
   undelivered and sent again on the next sweep, instead of sitting out twelve
   hours on a push nobody got.

4. **The health watchdog checks the JSON, not the HTTP status.** `POST
   /api/notify` answers 200 whether or not anything was delivered; the
   watchdog now reads `delivered`, falls back to ntfy directly, and only
   remembers a report it actually got out — an undelivered one is retried on
   the next run. Engine and notification connectivity are checked independently.
   Persistent incident keys prevent changing failure counts or unrelated
   recoveries from re-pushing a problem already reported.

What none of this can do: keep a closed MacBook awake. On AC with the lid shut
the Mac still sleeps between Power Nap dark-wakes (roughly hourly), and on
battery it sleeps a minute after the display goes off (`pmset -g custom`). Jobs
run late, once, when the machine is next awake — `--wake-at` schedules a daily
wake for the one that matters most.

### Repeatable recovery tests

```bash
pricewatch/.venv/bin/python -m pytest pricewatch/tests -q
python3 scripts/recovery-smoke.py --report /tmp/recovery-report.json
```

The Docker smoke test creates a temporary container with its own database, a
local Shopify-style store, and a notification relay that fails once. It freezes
the container across multiple scheduled ticks, verifies one catch-up and retry,
then restarts it to verify persistence and duplicate suppression. It removes the
test container and state afterwards. No real store or notification recipient is
used. This reproduces process suspension; a physical lid-close test is still
needed to verify this Mac's power, network, and launchd behavior together.

## Notes and caveats

- **Prices are geo-dependent.** Prusa and Bambu Lab redirect by IP; a run from
  Canada returns CAD. The currency is always reported alongside the price, and
  `PW_PREFERRED_CURRENCY` steers the engine at the storefront you actually buy
  from rather than leaving it to whatever IP the container has.
- **Conversions are indicative, never quoted.** The rates behind mixed-currency
  ranking and the `approx_in_preferred` hint are the ECB reference rate for the
  day (via Frankfurter, refreshed every six hours, cached across restarts), with
  the static table in `fx.py` as the fallback whenever that is unreachable. A
  reference rate is not what any card or store will charge; every hint names
  its source and day, `GET /api/fx` reports the table in force, and no tool
  ever reports a converted number as a store's price.
- **Scraping is best-effort.** Retailers change markup without warning. The
  verification harness is the tool for catching that — run it periodically.
- Respect the retailers: the default sweep is every 30 minutes with per-host rate
  limiting. Turning `PW_CHECK_CRON` down to the minute across 48 stores is both
  rude and a good way to get blocked.
- `PW_BROWSER_ENABLED=false` disables Chromium entirely if you want a lighter,
  HTTP-only deployment — you keep every Shopify/JSON-LD store (31 of the 48).
