---
name: price-tracking
description: Research prices across tech and 3D-printing retailers, check Reddit deal chatter, and set up price-drop alerts and scheduled checks. Use whenever the user asks what something costs, where it is cheapest, whether a deal is good, mentions a price seen on Reddit, asks to be told when a price falls, or wants to know when something is back in stock.
metadata:
  version: 1.4.0
---

# Price research and tracking

The `pricewatch` MCP server is the only source of price truth here. Never quote a
price from memory or from a general web search — call a tool and report what it
returns, with the store name and URL beside every figure.

## Choosing the right tool

| The user wants | Call |
| --- | --- |
| "What does X cost?" / "Where is X cheapest?" | `compare_prices` |
| A price for one specific page they linked | `get_price` |
| "Tell me when this link drops below $N" | `track_product_url` |
| "Watch X everywhere and alert me" | `track_product_description` |
| "Tell me when X is back in stock" | `track_product_url` / `track_product_description` with `alert_on_restock=true` |
| "What am I watching?" | `list_trackers` |
| "Is this actually a good price?" | `get_price_history` |
| "Check right now" | `refresh_prices_now` |
| "Reddit says people get these for $50" | `community_pulse`, then `read_reddit_thread` |
| "Check my prices more/less often" | `set_sweep_schedule` |
| "Did any alert actually fire?" | `list_alert_events` |
| "Quote me in CAD from now on" | `set_preferred_currency` |
| "What currency am I set to?" | `get_preferred_currency` |

## Identifying the product precisely

Cross-store matching is only as good as the description you pass. Before calling
`compare_prices` or `track_product_description`, make sure you have brand, model
**and** variant. "A Bambu printer" is not enough; "Bambu Lab P1S Combo with AMS"
is. If the user is vague, ask one clarifying question rather than guessing —
tracking the wrong variant produces confidently wrong alerts.

Watch for near-miss models: MK4S is not MK4, P1S is not P1P, and an A1 mini is not
an A1. If results come back mixing these, say so and confirm which one they meant.

Over-specifying does not break search anymore: when a fully detailed description
returns nothing, the engine automatically retries with the compact core (brand +
model) and reports what it searched in `search_terms_used`. So if a comparison
still comes back empty, the product genuinely is not in the store catalogs —
say that rather than retrying the same tool with reworded queries.

Some marketplaces are **search-only**: AliExpress prices appear in
`compare_prices` (frequently the cheapest, since OEMs sell direct there) but
its product pages cannot be re-read, so those listings cannot be tracked —
`track_product_description` will say so and list the matches. Offer to track
the same product at a supported store, and quote the AliExpress price as a
point-in-time comparison.

## Currency and region

The user has a currency they want to be answered in (`PW_PREFERRED_CURRENCY`,
changeable at any time with `set_preferred_currency`). The MCP server states it
in its instructions; `get_preferred_currency` reports it if you are unsure.
When one is set:

- **Lead with the stores that bill in it**, and link their regional storefront —
  amazon.ca rather than amazon.com for a Canadian shopper. The engine already
  searches the regional site where a store runs one, so those results come back
  natively in the right currency.
- **Never convert a price yourself.** Quote each store's real figure in the
  currency that store charges, and name the currency whenever it is not the
  user's.
- Results that come from a foreign-currency store carry an
  `approx_in_preferred` field. It is an *indicative* rate, not a rate of the
  day: show it as an approximation beside the real price ("$99 USD ≈ CA$136"),
  never in place of it, and never use it as an alert threshold.
- `compare_prices` reports `preferred_currency` and
  `results_in_preferred_currency`. If that count is zero, say so — "nothing in
  this comparison is priced in CAD" is useful information, not a failure.

- **A watch has one currency.** `track_*` records the currency of the listing
  the thresholds were written against (the user's own whenever a match bills
  in it) and reports it as `currency`. Alerts are measured only against
  listings in that currency; `list_trackers` counts the others under
  `foreign_listings` — they are shown for reference and never fire the watch,
  so a USD 99.99 on amazon.com is not "16% below" a CA$119 baseline. A pasted
  Amazon link is registered on the regional marketplace (amazon.ca) when the
  ASIN resolves there, and the result's `note` says so.

Target prices are taken in the currency of the listing being watched, so when
the user names a number, confirm which currency they mean if the cheapest
listing is foreign.

## Setting up an alert

Every watch needs at least one condition — the engine refuses one with none:

- `target_price` — absolute dollar threshold, best when the user names a number.
- `drop_pct` — percentage below the price at the moment the watch is created.
- `alert_on_restock=true` — tell the user when a sold-out listing comes back.

If the user says something loose like "tell me if it goes on sale", propose a
concrete rule (a 10–15% drop is a reasonable default) and confirm it rather than
silently picking one. After creating a watch, tell them the current price, the
condition, and which channels will notify them.

### Back-in-stock watches

`alert_on_restock=true` on either `track_*` tool fires once when a listing on
the watch goes from out of stock to in stock, naming the cheapest returned store
and its price. It is event-based: no cooldown, and no repeat until the listing
has sold out and come back again. The one-currency rule applies — a foreign
listing coming back never fires the watch. It combines freely with
`target_price` / `drop_pct`: each fires on its own terms, the restock alert
mentions a price condition the returned listing also meets, and
`list_alert_events` tags every event with `kind` (`price` or `restock`).

When a `track_*` result's `note` says the listing is out of stock today and the
user only asked for a price watch, offer the restock alert — a low price on an
unavailable item is not a deal — and switch it on with
`update_tracker(alert_on_restock=true)` rather than recreating the watch.
`list_trackers` reports `out_of_stock_listings` per watch.

Call `check_notifications` if the user is unsure whether alerts will reach them;
it reports which of Discord / Signal / WhatsApp are configured and can send a
test message. When **no channel is configured**, alerts still fire and are
recorded — surface them with `list_alert_events` — but nothing pushes to the
user. Say that plainly when you create a watch in that state.

## Community deal intel (Reddit)

When the user cites community chatter ("people on reddit are getting these for
$50"), do **not** hand-fetch reddit.com with curl — Reddit's JSON API blocks
this host and manual RSS scraping wastes dozens of steps. Use the tools:

- `community_pulse` — searches the deal subreddits (3Dprinting, BambuLab,
  3dbargains, buildapcsales by default) over RSS and returns recent posts,
  newest first, with any prices mentioned in the text.
- `read_reddit_thread` — opens one thread (post + top comments) to get the
  specifics: coupon code, region, expiry, whether the deal is dead.

Community prices are unverified leads. Confirm with `get_price` before
repeating one, and distinguish a time-boxed coupon ("$50 off until Monday,
Amazon US only") from a standing price — the difference decides whether the
right move is "buy now" or "set a tracker".

## Scheduling: trackers first, cron jobs second

Two schedulers exist. Choose deliberately:

1. **pricewatch trackers** (the default). Anything created with `track_*` is
   re-checked automatically on the engine's sweep schedule and alerts through
   the configured channels. For "watch this price" requests, creating the
   tracker **is** the complete setup — no cron job needed. If the user wants
   faster or slower re-checks, call `set_sweep_schedule` (5-field cron, UTC;
   the engine refuses anything more frequent than every 5 minutes).
2. **hermes cron jobs** (the `cronjob` tool) — only for work the price engine
   cannot do by itself: a periodic Reddit sweep with `community_pulse`, a
   watchdog for a store the engine reports as blocked (see the
   price-watch-fallback skill's no_agent script pattern), or a recurring
   morning deals briefing.

After creating a cron job, verify it: run `cronjob action=list` and confirm the
job shows as scheduled with a next-run time. If the cronjob tool is not
available in your session, say so — do not silently fall back to pretending a
watch exists.

Delivery honesty: cron output without a connected messaging platform is
**local-only** (`hermes cron runs` shows it; the user sees results on their
next chat, not as a push). Pricewatch tracker alerts push only through the
channels `check_notifications` reports as configured. Whenever you set up
either kind of schedule, state exactly how — and whether — the user will be
notified.

## How the engine fetches (don't reinvent it)

The MCP tools already handle anti-bot fetching for you — there is no need to open
a terminal and `curl` a store yourself, and doing so is less reliable than the
engine. Under the hood every read goes through one ladder, cheapest rung first:

1. **HTTP with a real-browser TLS/HTTP2 fingerprint.** This clears the common
   walls (Cloudflare "Just a moment", Akamai, and Amazon) in about a second —
   which is why `get_price` on an Amazon URL now returns a price with no Keepa
   key for most listings.
2. **Headless browser**, only for genuinely JS-rendered pages.
3. **Give up fast** for the few stores whose walls key on IP reputation.

The engine remembers which rung worked for each store (a per-host "playbook"), so
repeat lookups skip straight to it instead of re-probing. You do **not** need to
figure out how to reach a store — just call the tool once and read the result.

What this means when a read fails:

- **Micro Center, Adorama, Mouser, Target** need a residential proxy
  (`PW_HTTP_PROXY`) or an official API — retrying won't help. Report the store as
  blocked and move on; don't loop on it.
- **Best Buy** and **eBay** are most reliable with their free API keys; without
  them coverage is partial.
- **Amazon** reads over HTTP now; `KEEPA_API_KEY` is only needed for hardened
  pages or heavy use, not the common case.

Call `engine_status` to see the current fingerprint, whether a proxy/keys are
configured, and the learned playbook — use it to explain *why* a store failed
rather than guessing or hand-fetching.

## Reporting results

- Lead with the cheapest in-stock option, then list the alternatives.
- Always include the store and a link.
- Flag when something is out of stock — a low price on an unavailable item is not
  a deal.
- If a store returns `NEEDS-KEY` or an error, say which store failed and why
  instead of quietly dropping it from the comparison. `engine_status` explains
  what is configured.
- Prices come from different stores in different currencies; always state the
  currency when it is not the user's preferred one (see "Currency and region").

## Honesty rules

Do not estimate, interpolate, or "remember" a price. If a tool cannot read a
price, report that it could not be read. A missing price is a fine answer; a
made-up one is not.
