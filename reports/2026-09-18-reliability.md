# Shopping agent reliability verification — 2026-09-18 (Toronto)

Changes were built on the wake-recovery work already present in the checkout.
The engine was rebuilt and Hermes reconnected; production trackers were kept.

## Findings and fixes

- A fresh engine now runs an immediate catch-up sweep. Frozen-process cron
  slots coalesce to one execution; an unavailable DNS resolver causes a
  bounded wait and a retry sixty seconds later, without moving the normal cron.
- Concurrent full refreshes share one task. A disconnected caller cannot cancel
  it. Shutdown waits for that task to cancel before closing HTTP clients.
  Alert evaluation is serialized so concurrent targeted/full sweeps do not
  both send the same matching alert.
- Notifications retry DNS/connect failures, rate limits and temporary server
  failures. Permanent HTTP errors fail immediately. Long Retry-After values
  defer additional attempts (including later dispatches in the same process).
  Failed delivery retains the watch's eligibility; accepted delivery starts
  the cooldown. This is best-effort delivery, not a transactional outbox.
- Shopify currency/market discovery no longer permanently caches a failed
  network lookup as a missing currency or market.
- The Mac installer now uses StartCalendarInterval. The local macOS
  launchd.plist manual says StartInterval drops events missed during sleep,
  while calendar events coalesce on wake. Kernel wake timestamps detect short
  sleeps too. Failed host recovery remains pending, and unhealthy running
  containers are restarted before health verification.
- The store verifier now honors --no-browser throughout extraction, treats
  timeouts as strict failures, and rejects invalid store names/concurrency.

## Checks completed

- Offline regression suite: **348 passing locally and in a disposable Linux container with
  the final image’s installed dependencies**. Only deprecation warnings were reported.
- Full live catalog: **33 of 48 stores returned a price**, both before and
  after the first deployment. One product per store was discovered or seeded;
  these checks validate extraction, not every variant or checkout price.
- Regional search: **13 of 14 returned CAD listings** for PLA. Elegoo, Anycubic,
  QIDI, Creality, Polymaker, Voxel Factory, 3D Printing Canada, Filaments.ca,
  DigitMakers, Shop3D, Best Buy Canada, Canada Computers and Amazon returned
  results. Spool3D returned none. Some store search endpoints are broad (e.g.
  QIDI returned plates); price comparison applies its own relevance ranking.
- Isolated real Docker freeze: twelve seconds across multiple five-second
  test ticks. Exactly one immediate catch-up, in **0.34 seconds** on the final rerun (0.19 seconds initially). A local
  Shopify product fell from CAD 20 to CAD 9; the actual notification transport
  received HTTP 503 then HTTP 200. Container restart refreshed immediately
  and preserved the delivered-alert watermark, with no second notification.
- Real ntfy delivery: one clearly labelled test notification accepted through
  /api/notify and independently observed in the configured ntfy subscriber
  feed. Phone display/receipt was not observed. Discord, Signal and WhatsApp
  are not configured, so they were not exercised against real providers.
- Actual Hermes model/MCP: a read-only request reached engine_status,
  list_trackers and notification status; it reported the successful seven-
  listing sweep and five active watches after reconnection.
- Host launch agent: installed and observed completing recovery automatically
  at 22:22–22:24 EDT. All three containers healthy, all seven production
  listings refreshed, zero failures, zero alerts; later idle ticks exit 0.
- Actual stopped-stack recovery: stopped the production pricewatch and Hermes
  containers, then waited for the installed calendar helper without manually
  starting either one. **Both services recovered and all seven listings
  refreshed in 34.2 seconds**, zero failed reads and zero alerts. Signal
  remained healthy throughout. See [host recovery evidence](2026-09-18-host-recovery.json).

## Physical sleep test

The isolated Docker pause/restart test is complete. **A new physical lid-close
and wake cycle has not yet been performed during this verification.** The Mac
helper's launch, recovery of stopped services, and catch-up have been verified
while awake. Existing macOS
power logs show earlier lid-close sleeps, but those predate this deployment
and do not prove the new helper works across a physical sleep.

To complete the hardware check: close the lid for approximately two minutes,
reopen it, then check `scripts/host/install-wake-agent.sh --status`, the wake
log, and `/api/health`. Expect a new successful catch-up after the kernel wake
time changes, healthy containers, and a fresh seven-listing sweep. If a macOS
Python/Documents permission prompt appears, the user must approve it for the
background helper to read this project. It initially stalled opening the
script, then proceeded and completed successfully during this run.

## Live store results after deployment

Raw results: [before](2026-09-18-stores-before.json),
[after](2026-09-18-stores-after.json), and
[Docker recovery events](2026-09-18-docker-recovery.json).

| Store | Result | Observed price | Method / limitation |
|---|---|---:|---|
| 3D Printing Canada | OK | 156,000.00 CAD | shopify-json |
| 3DJake | OK | 31.66 EUR | http:selector |
| Adorama | BLOCKED | — | adorama.com is known to block automated fetches — set PW_HTTP_PROXY (residential) or a store API key |
| AliExpress | LIMITED | — | AliExpress product pages block automated reads from this host. Current prices are available via compare_prices (search), but this listing cannot be tracked — of |
| Amazon | OK | 140.07 CAD | http:amazon-dom |
| Anycubic | OK | 38.80 USD | shopify-json |
| Atomic Filament | OK | 34.49 USD | shopify-json |
| B&H Photo | BLOCKED | — | blocked |
| Bambu Lab | OK | 6.99 CAD | http:json-ld |
| Best Buy | NEEDS-KEY | — | BESTBUY_API_KEY not set (free at developer.bestbuy.com) |
| Best Buy Canada | OK | 999.99 CAD | bestbuyca-api |
| Canada Computers | OK | 719.99 CAD | http:json-ld |
| Creality | OK | 599.00 USD | http:json-ld |
| DigiKey | BLOCKED | — | digikey.com is known to block automated fetches — set PW_HTTP_PROXY (residential) or a store API key |
| DigitMakers | OK | 91.99 CAD | shopify-json |
| E3D Online | OK | 41.50 GBP | shopify-json |
| eBay | NO-URL | — | not-found |
| Elegoo | OK | 329.00 USD | shopify-json |
| Fabreeko | OK | 35.47 CAD | shopify-json |
| Filaments.ca | OK | 12.99 CAD | shopify-json |
| Filastruder | OK | 39.99 USD | shopify-json |
| Fillamentum | OK | 46.50 EUR | shopify-json |
| Gulf Coast Robotics | TIMEOUT | — | timeout |
| KB-3D | NO-URL | — | not-found |
| LDO Motors | FAIL | — | no price in page |
| MatterHackers | OK | 5,799.00 USD | browser:json-ld |
| Memory Express | BLOCKED | — | blocked |
| Micro Center | BLOCKED | — | microcenter.com is known to block automated fetches — set PW_HTTP_PROXY (residential) or a store API key |
| Micro Swiss | OK | 24.00 CAD | shopify-json |
| Mouser | FAIL | — | no price in page |
| Newegg | OK | 799.99 USD | http:selector |
| Overture | OK | 26.99 USD | shopify-json |
| Polymaker | OK | 62.97 USD | shopify-json |
| Printed Solid | OK | 37.99 USD | shopify-json |
| Proto-pasta | OK | 5.00 USD | shopify-json |
| Prusa Research | OK | 3,329.00 CAD | http:json-ld |
| QIDI Tech | OK | 4.99 USD | shopify-json |
| Shop3D.ca | OK | 15.99 CAD | shopify-json |
| Siboor | BLOCKED | — | blocked |
| Slice Engineering | OK | 134.99 USD | shopify-json |
| Sovol 3D | OK | 1,499.00 USD | shopify-json |
| Spool3D | OK | 3.00 CAD | http:json-ld |
| SUNLU | NO-URL | — | not-found |
| Target | BLOCKED | — | bot challenge |
| TH3D Studio | OK | 27.99 USD | browser:json-ld |
| Voxel Factory | OK | 23.99 CAD | shopify-json |
| Walmart | OK | 199.00 USD | http:next-data |
| West3D | OK | 23.00 CAD | shopify-json |

Final tally: 33 OK, 7 BLOCKED, 2 FAIL (no price), 3 NO-URL, 1 TIMEOUT,
1 NEEDS-KEY, 1 LIMITED. Mouser changed from BLOCKED to a page without a price
between runs; successful store coverage stayed unchanged. Access challenges
were reported as limitations; none were bypassed.

Repeat the offline suite with `pricewatch/.venv/bin/python -m pytest pricewatch/tests -q`.
Repeat the isolated Docker test with `python3 scripts/recovery-smoke.py`.
Repeat live store extraction with `docker compose exec -T pricewatch python -m pricewatch.verify`.
