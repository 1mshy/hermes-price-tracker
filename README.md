# Hermes Shopping

A price-tracking shopping assistant: **NousResearch [Hermes Agent](https://hermes-agent.nousresearch.com)**
in Docker, talking to your own model, wired over MCP to a price engine that reads
live prices from tech and 3D-printing retailers and pings you on Discord, Signal
or WhatsApp when something gets cheap.

```
┌────────────┐   MCP over HTTP    ┌─────────────────────────────┐
│  hermes    │ ─────────────────► │  pricewatch                 │
│  (agent)   │                    │  • 39 store adapters        │
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
python -m pricewatch.verify`). Latest run (2026-08-27): **27 of 39 stores returned a live price** (the rest: 4 documented IP-reputation walls, 1 keyless API, 1 search-only marketplace, and stores with no machine-readable price).

**Working without any API key (22 of them 3D printing):**

| | |
|---|---|
| **Shopify JSON** (exact price, in-stock, variants) | Elegoo · Anycubic · Printed Solid · E3D · Slice Engineering · Micro Swiss · West3D · Fabreeko · Filastruder · QIDI · Sovol · Polymaker · Proto-pasta · Atomic Filament · Overture · Fillamentum |
| **schema.org JSON-LD over fingerprinted HTTP** | Bambu Lab · Prusa · Creality · B&H Photo · DigiKey |
| **Fingerprinted HTTP + DOM/buy-box** | Amazon · Walmart · Newegg · 3DJake |
| **Headless browser + JSON-LD** | MatterHackers · TH3D |

**The fast path is a browser TLS fingerprint.** Retail bot walls (Cloudflare,
Akamai, Amazon) fingerprint the TLS handshake and HTTP/2 settings, not the header
set — so plain `httpx` gets challenged even with perfect headers. The engine
sends a real Chrome fingerprint (via `curl_cffi`, `PW_IMPERSONATE`), which clears
most walls in ~1s with no browser. **Amazon, Walmart, Newegg, B&H and DigiKey now
read over HTTP with no key and no proxy.** A headless browser is the fallback for
genuinely JS-rendered pages only.

**Marketplaces** — eBay now reads keylessly (fingerprinted HTTP + JSON-LD on
item pages, HTML parsing for search); `EBAY_APP_ID` + `EBAY_CERT_ID` remain the
most reliable path. AliExpress is **search-only**: `compare_prices` returns live
prices from its search page (often the cheapest source — OEMs like SUNLU sell
direct), in whichever currency `PW_PREFERRED_CURRENCY` pins the locale to, but
product pages block automated reads, so AliExpress listings cannot be tracked
and the tools say so instead of failing silently.

**Needs a free API key** — Best Buy (`BESTBUY_API_KEY`) requires its key.
`KEEPA_API_KEY` makes Amazon bulletproof but is no longer required for everyday
lookups.

**Still IP-walled** — Micro Center, Adorama, Mouser, Target key on IP reputation
and reject even a real browser from a datacenter address. Point `PW_HTTP_PROXY`
at a residential proxy; the adapters use it the moment it is configured, and until
then they fail fast with a clear reason instead of stalling on Chromium.

The engine caches which rung worked per host (a fetch "playbook", visible in
`engine_status`), so repeat lookups skip straight to it instead of re-probing —
the agent never has to rediscover how to reach a store.

**Not supported** — SUNLU, KB-3D, LDO Motors, Siboor, Gulf Coast Robotics publish
no machine-readable price (or block outright). They stay in the catalog so a
pasted URL gets a clear error rather than a silent miss.

`list_stores` (or `GET /api/stores`) reports each store's method and limitation,
so the agent can explain gaps instead of quietly dropping a retailer.

## How prices are read

Preference order, per store — cheapest and most reliable first:

1. **Official API** — Best Buy, eBay, Keepa. Allowed, stable, no scraping.
2. **Platform JSON** — Shopify `/products/<handle>.js` (falling back to the
   older `.json` view on themes that gate it), WooCommerce Store API.
   Public storefront endpoints, exact prices in minor units, no HTML parsing.
3. **Fingerprinted HTTP** — a GET with a real Chrome TLS/HTTP2 fingerprint
   (`curl_cffi`), then schema.org JSON-LD / microdata / OpenGraph, Amazon's
   buy-box JSON, or site DOM selectors. This is the workhorse for the big-box
   retailers and clears most bot walls without a browser.
4. **Headless Chromium** — only when the price is rendered client-side or a wall
   fingerprints deeper than TLS.
5. **Residential proxy** — the last resort for IP-reputation walls, via
   `PW_HTTP_PROXY`.

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
listing does not outrank a US$99 one. That has not changed — the rates are
static and for ranking and orientation only, never for quoting.

## Alerts

A watch fires when either condition is met:

- `target_price` — absolute, e.g. alert under $250.
- `drop_pct` — relative to the price when the watch was created.

Both are in the **watch's currency** — that of the listing the watch was created
from (the user's own whenever a match bills in it). Only listings in that
currency are measured against them; a listing in another currency stays on the
watch for reference, and `list_trackers` counts it under `foreign_listings`, but
it never fires the alert. USD 99.99 is not "16% below" a CA$119 baseline, and
amazon.com shows a Canadian visitor a *converted* figure that reads CAD on one
fetch and USD on the next — which is why a pasted Amazon link is registered on
the regional marketplace (amazon.ca) whenever the ASIN resolves there.

Alerts are deduplicated by a per-tracker cooldown (default 12 h) that is bypassed
only when the price falls *further* than the last alert; once it expires, a
repeat goes out only if the price has moved since the last one, so a slow slide
keeps notifying while a flat price stays quiet.

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
job that speaks up only when the engine is down or a tracked listing has
failed 3+ sweeps in a row).

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

Useful commands:

```bash
docker compose exec hermes hermes cron list     # what's scheduled, next runs
docker compose exec hermes hermes cron runs     # execution history
docker compose exec hermes hermes cron status   # is the ticker alive
```

Delivery: with no messaging platform connected, job output is local-only (the
user sees it on their next chat; `cron runs` shows it). Connect a platform
with `hermes gateway setup` for push delivery — or just use pricewatch
trackers, which push through Discord/Signal/WhatsApp on their own.

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
./scripts/rebuild.sh all             # everything
./scripts/rebuild.sh --smoke         # also run scripts/agent-smoke.sh
```

After a pricewatch rebuild the script also recreates `hermes`, whose open
dashboard session otherwise holds an MCP stream to the container that just went
away.

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
`selector`, add XPaths to `SELECTOR_RULES` in `stores/registry.py`. Then verify it:
`python -m pricewatch.verify --store mystore`.

## Notes and caveats

- **Prices are geo-dependent.** Prusa and Bambu Lab redirect by IP; a run from
  Canada returns CAD. The currency is always reported alongside the price, and
  `PW_PREFERRED_CURRENCY` steers the engine at the storefront you actually buy
  from rather than leaving it to whatever IP the container has.
- **Conversions are indicative, never quoted.** The rates in `money.py` are
  static, hand-maintained approximations used for ranking mixed-currency results
  and for the `approx_in_preferred` hint. They are not a rate of the day; no
  tool ever reports a converted number as a store's price.
- **Scraping is best-effort.** Retailers change markup without warning. The
  verification harness is the tool for catching that — run it periodically.
- Respect the retailers: the default sweep is every 30 minutes with per-host rate
  limiting. Turning `PW_CHECK_CRON` down to the minute across 38 stores is both
  rude and a good way to get blocked.
- `PW_BROWSER_ENABLED=false` disables Chromium entirely if you want a lighter,
  HTTP-only deployment — you keep every Shopify/JSON-LD store (22 of the 25).
