---
name: price-tracking
description: Research prices across tech and 3D-printing retailers and set up price-drop alerts. Use whenever the user asks what something costs, where it is cheapest, whether a deal is good, or asks to be told when a price falls.
metadata:
  version: 1.0.0
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
| "What am I watching?" | `list_trackers` |
| "Is this actually a good price?" | `get_price_history` |
| "Check right now" | `refresh_prices_now` |

## Identifying the product precisely

Cross-store matching is only as good as the description you pass. Before calling
`compare_prices` or `track_product_description`, make sure you have brand, model
**and** variant. "A Bambu printer" is not enough; "Bambu Lab P1S Combo with AMS"
is. If the user is vague, ask one clarifying question rather than guessing —
tracking the wrong variant produces confidently wrong alerts.

Watch for near-miss models: MK4S is not MK4, P1S is not P1P, and an A1 mini is not
an A1. If results come back mixing these, say so and confirm which one they meant.

## Setting up an alert

Every watch needs at least one condition:

- `target_price` — absolute dollar threshold, best when the user names a number.
- `drop_pct` — percentage below the price at the moment the watch is created.

If the user says something loose like "tell me if it goes on sale", propose a
concrete rule (a 10–15% drop is a reasonable default) and confirm it rather than
silently picking one. After creating a watch, tell them the current price, the
condition, and which channels will notify them.

Call `check_notifications` if the user is unsure whether alerts will reach them;
it reports which of Discord / Signal / WhatsApp are configured and can send a
test message.

## Reporting results

- Lead with the cheapest in-stock option, then list the alternatives.
- Always include the store and a link.
- Flag when something is out of stock — a low price on an unavailable item is not
  a deal.
- If a store returns `NEEDS-KEY` or an error, say which store failed and why
  instead of quietly dropping it from the comparison. `engine_status` explains
  what is configured.
- Prices come from different stores in different currencies; state the currency
  when it is not USD.

## Honesty rules

Do not estimate, interpolate, or "remember" a price. If a tool cannot read a
price, report that it could not be read. A missing price is a fine answer; a
made-up one is not.
