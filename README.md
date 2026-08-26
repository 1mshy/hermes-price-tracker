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
cp .env.example .env          # then edit: model endpoint + notification channels
docker compose up -d pricewatch
docker compose run --rm hermes          # interactive agent
```

Then just talk to it:

> *"What does a Bambu Lab P1S Combo cost right now?"*
> *"Track https://store.creality.com/products/k1-se-3d-printer and tell me if it drops 12%."*
> *"Watch for Polymaker PolyTerra PLA 1kg matte black under $18 anywhere."*
> *"What am I tracking, and has anything moved?"*

## What actually works

Verified live against every store in the catalog (`docker compose exec pricewatch
python -m pricewatch.verify`). Latest run: **25 of 38 stores returned a live price.**

**Working without any API key (22 of them 3D printing):**

| | |
|---|---|
| **Shopify JSON** (exact price, in-stock, variants) | Elegoo · Anycubic · Printed Solid · E3D · Slice Engineering · Micro Swiss · West3D · Fabreeko · Filastruder · QIDI · Sovol · Polymaker · Proto-pasta · Atomic Filament · Overture · Fillamentum |
| **schema.org JSON-LD** | Bambu Lab · Prusa · Creality |
| **Headless browser + JSON-LD** | MatterHackers · B&H Photo · DigiKey · TH3D |
| **Headless browser + DOM selectors** | Newegg · 3DJake |

**Needs a free API key** — Best Buy (`BESTBUY_API_KEY`), eBay (`EBAY_APP_ID` +
`EBAY_CERT_ID`). Both are implemented; add the key and they start working.

**Bot-walled, will not work by scraping** — Amazon, Walmart, Target, Micro Center,
Adorama, Mouser. These reject even a real headless Chromium from a normal IP.
Honest options: set `KEEPA_API_KEY` for Amazon (paid, reliable), or point
`PW_HTTP_PROXY` at a residential proxy. The adapters are written and will use
either the moment it is configured.

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
3. **Structured data** — schema.org JSON-LD (including `ProductGroup`/`hasVariant`,
   which modern Shopify themes use), microdata, then OpenGraph.
4. **Headless Chromium** — only when plain HTTP is blocked or the price is
   rendered client-side.
5. **Site-specific DOM selectors** — last resort, for stores with no structured
   data at all (Newegg, 3DJake).

Requests are rate-limited per hostname (`PW_PER_HOST_RPS`, default 0.4/s) with
jitter. Unknown domains are sniffed automatically — paste a link to any Shopify
or WooCommerce store and it will usually just work.

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

## Model configuration

`hermes/entrypoint.sh` renders `~/.hermes/config.yaml` from `.env` on every start,
so the container stays declarative — change `.env`, restart, done.

Three settings matter for a self-hosted endpoint, and all three are load-bearing:

| Setting | Why |
|---|---|
| `LLM_CONTEXT_LENGTH=65536` | Hermes assumes a 256K window when it cannot detect one, and refuses to run below 64K. Set your server's real `max_model_len`. |
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
