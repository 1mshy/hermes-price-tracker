# Hermes Shopping

A price-tracking shopping assistant: **NousResearch [Hermes Agent](https://hermes-agent.nousresearch.com)**
in Docker, talking to your own model, wired over MCP to a price engine that reads
live prices from tech and 3D-printing retailers and pings you on Discord, Signal
or WhatsApp when something gets cheap.

```
┌────────────┐   MCP over HTTP    ┌─────────────────────────────┐
│  hermes    │ ─────────────────► │  pricewatch                 │
│  (agent)   │                    │  • 38 store adapters        │
└─────┬──────┘                    │  • price history (SQLite)   │
      │ OpenAI API                │  • scheduled sweeps         │
      ▼                           │  • alert rules              │
 http://10.150.0.30:1234/v1       └──────────┬──────────────────┘
 (vLLM · Qwen3.8-27B)                        │
                                   Discord / Signal / WhatsApp
```

## Quick start

```bash
cp .env.example .env          # then edit: model endpoint, dashboard password, channels
docker compose up -d pricewatch dashboard gateway
docker compose run --rm hermes          # interactive agent in the terminal
```

(`gateway` is the cron ticker — without it, scheduled agent jobs never fire;
see *Scheduled agent jobs* below.)

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
model/provider/API-key config without touching a file. `docker compose up -d
dashboard` runs it on **http://localhost:9119**, wired to the same config and the
same `pricewatch` tools as the terminal agent, so you can do all the price work
from the browser.

It binds to `0.0.0.0` inside the container so you can reach it from your machine,
and Hermes refuses to do that without an auth provider. Set a password in `.env`:

```ini
HERMES_DASHBOARD_BASIC_AUTH_USERNAME=admin
HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=<choose one>
HERMES_DASHBOARD_BASIC_AUTH_SECRET=<random string>   # keeps you signed in across restarts
```

Without a password the container exits with an explanatory error rather than
starting something unprotected. The UI build ships prebuilt in the wheel, so the
service runs with `--skip-build` and needs no npm step.

**Exposing it beyond localhost:** the published port is bound on your machine. If
you want it reachable from elsewhere, put it behind a tunnel or reverse proxy with
TLS rather than opening port 9119 — the session cookie is only as safe as the
transport.

Useful commands:

```bash
docker compose logs -f dashboard      # startup + request log
docker compose restart dashboard      # pick up .env changes
```

## What actually works

Verified live against every store in the catalog (`docker compose exec pricewatch
python -m pricewatch.verify`). Latest run: **25 of 38 stores returned a live price.**

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

**Needs a free API key** — Best Buy (`BESTBUY_API_KEY`), eBay (`EBAY_APP_ID` +
`EBAY_CERT_ID`) are most reliable with their keys. `KEEPA_API_KEY` makes Amazon
bulletproof but is no longer required for everyday lookups.

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
2. **Platform JSON** — Shopify `/products/<handle>.js`, WooCommerce Store API.
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
usually just work.

## Alerts

A watch fires when either condition is met:

- `target_price` — absolute, e.g. alert under $250.
- `drop_pct` — relative to the price when the watch was created.

Alerts are deduplicated by a per-tracker cooldown (default 12 h) that is bypassed
only when the price falls *further* than the last alert, so a slow slide keeps
notifying while a flat price stays quiet.

Configure channels in `.env`:

- **Discord** — `DISCORD_WEBHOOK_URL`. Easiest; no bot needed.
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

## Scheduled agent jobs (hermes cron)

Price watches don't need cron — trackers re-check themselves on the sweep
schedule. Hermes cron is for what the engine can't do alone: a morning Reddit
deals briefing, or a silent no-LLM watchdog script for a store the engine
reports as blocked (see `hermes/skills/price-tracking/price-watch-fallback/`).

Two pieces make agent-created cron jobs actually work, and both are wired into
`docker-compose.yml`:

1. **`HERMES_INTERACTIVE=1`** on the `hermes` and `dashboard` services — the
   agent's `cronjob` tool is gated on an interactive-capable session and never
   loads without it.
2. **The `gateway` service** — hermes' cron ticker only runs inside a gateway
   process (`hermes cron status` says exactly this). It shares the agent home
   volume, so jobs created from the dashboard or terminal fire here.

Useful commands:

```bash
docker compose exec gateway hermes cron list     # what's scheduled, next runs
docker compose exec gateway hermes cron runs     # execution history
docker compose exec gateway hermes cron status   # is the ticker alive
```

Delivery: with no messaging platform connected, job output is local-only (the
user sees it on their next chat; `cron runs` shows it). Connect a platform
with `hermes gateway setup` for push delivery — or just use pricewatch
trackers, which push through Discord/Signal/WhatsApp on their own.

## Model configuration

`hermes/entrypoint.sh` renders `~/.hermes/config.yaml` from `.env` on every start,
so the container stays declarative — change `.env`, restart, done.

Three settings matter for a self-hosted endpoint, and all three are load-bearing:

| Setting | Why |
|---|---|
| `LLM_CONTEXT_LENGTH=131072` | Hermes assumes a 256K window when it cannot detect one, and refuses to run below 64K. Set your server's real `max_model_len` (`curl $LLM_BASE_URL/models` reports it). |
| `LLM_MAX_TOKENS=8192` | Left alone, Hermes requests `max_tokens == context_length`; vLLM requires `prompt + max_tokens <= max_model_len`, so every call would fail once and retry. |
| `mcp>=1.9,<2` (in the image) | hermes-agent 0.19 imports `streamablehttp_client`, which mcp 2.x renamed. The price engine serves MCP with 2.x — separate images, no conflict. |

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

## Running the tests

The engine has an offline unit suite (price parsing, product matching, HTML
extraction, Reddit feed parsing, alert rules, schedule validation — no network):

```bash
cd pricewatch
uv venv .venv && uv pip install -p .venv/bin/python -e . pytest
.venv/bin/python -m pytest tests/ -q
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
  Canada returns CAD. The currency is always reported alongside the price.
- **Scraping is best-effort.** Retailers change markup without warning. The
  verification harness is the tool for catching that — run it periodically.
- Respect the retailers: the default sweep is every 30 minutes with per-host rate
  limiting. Turning `PW_CHECK_CRON` down to the minute across 38 stores is both
  rude and a good way to get blocked.
- `PW_BROWSER_ENABLED=false` disables Chromium entirely if you want a lighter,
  HTTP-only deployment — you keep every Shopify/JSON-LD store (22 of the 25).
