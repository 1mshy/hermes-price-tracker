"""The reasoning half of the swarm.

Three roles, each with a deterministic fallback, and one hard rule between
them: **the model never produces a number that reaches the report.** Prices,
distances, odometers and deal scores all come from `valuation`; the model reads
free text, judges qualitative things, and writes prose. That division is what
makes the report reproducible and lets a wrong figure be traced to a formula.

* `parse_request` — a sentence into a search. Handles the phrasings the regex
  parser in `spec` does not, and its output is merged over that parser rather
  than replacing it, so a model failure degrades instead of breaking.
* `assess` — several agents in parallel, each reading a handful of listings and
  pulling out what the seller actually said: equipment, condition claims, red
  flags. Most of this market advertises in French, which is exactly the kind of
  reading a model does better than a regex.
* `narrate` — the opening paragraphs of the report, written from a digest of
  the computed facts.
"""
from __future__ import annotations

import asyncio
import logging

from . import llm, spec
from .listing import CarListing
from .swarm import SwarmResult
from .valuation import Analysis

log = logging.getLogger(__name__)

# Token budgets, sized for `llm.chat`'s no-thinking default. They still leave
# roughly 4x headroom over the measured answer length, because a truncated
# completion is worth nothing at all.
PARSE_TOKENS = 1200
ASSESS_TOKENS = 4500
NARRATE_TOKENS = 1500

#: Listings per assessor agent — small enough to keep each prompt sharp.
CHUNK = 5
#: How many assessor agents may be in flight at once.
#:
#: One, deliberately. The scouts fan out to five different websites, so running
#: those together is free parallelism; the assessors all queue on the same
#: model. Firing them concurrently does not finish sooner — one endpoint serves
#: them at a fixed rate either way — and it makes each request slower, closer
#: to its timeout, for no gain.
ASSESS_CONCURRENCY = 1
#: never send the model an unbounded shortlist
MAX_ASSESSED = 16


# ── role 1: read the request ────────────────────────────────────────────
_PARSE_SYSTEM = (
    "You turn a shopper's sentence about buying a used car into JSON. "
    "Reply with JSON only, no prose.\n"
    "Keys: make, model, year_min, year_max, location, radius_km, price_max, "
    "price_min, max_mileage_km, trim, include_salvage.\n"
    "Rules: omit any key you cannot determine — never guess. `location` is the "
    "place the buyer is searching from, as a plain place name. A single model "
    "year sets both year_min and year_max. Use numbers, not strings, for "
    "numeric keys. Never invent a make or model that is not in the sentence."
)


async def parse_request(text: str) -> dict:
    """Free text to search fields: regex first, model layered over the gaps.

    The deterministic parser wins on anything it is confident about, because it
    cannot hallucinate an Audi into a sentence about a Volkswagen.
    """
    baseline = spec.parse_free_text(text)
    if not llm.available():
        return baseline

    parsed = await llm.chat_json(
        [{"role": "system", "content": _PARSE_SYSTEM},
         {"role": "user", "content": text}], max_tokens=PARSE_TOKENS)
    if not isinstance(parsed, dict):
        return baseline

    allowed = {"make", "model", "year_min", "year_max", "location", "radius_km",
               "price_max", "price_min", "max_mileage_km", "trim", "include_salvage"}
    merged = dict(baseline)
    for key, value in parsed.items():
        if key not in allowed or value in (None, "", []):
            continue
        if key in ("year_min", "year_max", "max_mileage_km"):
            value = _as_int(value)
        elif key in ("radius_km", "price_max", "price_min"):
            value = _as_float(value)
        if value is None:
            continue
        # The regex parser is authoritative where it fired: it read the literal
        # tokens, so it cannot substitute a different marque.
        if key in ("make", "model") and baseline.get(key):
            continue
        merged.setdefault(key, value) if key in baseline else merged.update({key: value})
    return merged


def _as_int(value):
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _as_float(value):
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


# ── role 2: read the listings ───────────────────────────────────────────
_ASSESS_SYSTEM = (
    "You read used-car listings and report only what the seller actually wrote. "
    "Many are in Quebec French; read those as fluently as the English ones.\n"
    "Reply with a JSON array. One object per listing, in the order given, each: "
    '{"id": <the id given>, "equipment": [short tags], "condition_notes": [short '
    'phrases], "flags": [short warnings]}.\n'
    "Rules: `equipment` is notable kit the seller names (leather, sunroof, "
    "quattro, CarPlay, heated seats). `condition_notes` is what they claim about "
    "the car's state (accident-free, one owner, certified, new brakes). `flags` "
    "is anything a buyer should be wary of that is stated or strongly implied "
    "(accident history, rebuilt or salvage title, very high mileage for the "
    "year, sold as-is, no warranty, needs work).\n"
    "The array MUST contain exactly one object for every listing you were "
    "given, in the same order, even when a listing is terse and all three of "
    "its lists come back empty. An empty inner list is fine; an empty array is "
    "never a valid answer. Never infer a fact that is not in the text. Do not "
    "comment on price. Keep every string under 6 words. JSON only."
)


def _listing_digest(item: CarListing, index: int) -> dict:
    return {
        "id": index,
        "title": item.title[:180],
        "trim": item.trim,
        "year": item.year,
        "odometer_km": item.mileage_km,
        "seller_type": item.seller_type,
        "source": item.source,
    }


async def assess(listings: list[CarListing]) -> dict[int, dict]:
    """Fan several reader agents across the shortlist, concurrently.

    Keyed by the listing's index in the list passed in. Anything the model
    fails to return simply has no assessment; nothing downstream requires one.
    """
    if not llm.available() or not listings:
        return {}

    subset = listings[:MAX_ASSESSED]
    chunks = [subset[i:i + CHUNK] for i in range(0, len(subset), CHUNK)]

    async def one(offset: int, chunk: list[CarListing],
                  temperature: float = 0.1) -> dict[int, dict]:
        payload = [_listing_digest(item, offset + i) for i, item in enumerate(chunk)]
        import json
        result = await llm.chat_json(
            [{"role": "system", "content": _ASSESS_SYSTEM},
             {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            temperature=temperature, max_tokens=ASSESS_TOKENS)
        out: dict[int, dict] = {}
        if result is None:
            log.warning("assessor chunk at offset %s returned nothing usable", offset)
        if isinstance(result, dict):
            result = result.get("listings") or result.get("results") or []
        if not isinstance(result, list):
            return out
        for position, entry in enumerate(result):
            if not isinstance(entry, dict):
                continue
            index = _as_int(entry.get("id"))
            if index is None or not (offset <= index < offset + len(chunk)):
                # Models given ids 8..15 often answer 0..7 anyway. The order is
                # specified, so fall back to position rather than discarding a
                # whole chunk's work over a renumbering.
                index = offset + position
            if not (offset <= index < offset + len(chunk)):
                continue
            out[index] = {
                "equipment": _strings(entry.get("equipment")),
                "condition_notes": _strings(entry.get("condition_notes")),
                "flags": _strings(entry.get("flags")),
            }
        return out

    limit = asyncio.Semaphore(ASSESS_CONCURRENCY)

    async def bounded(offset: int, chunk: list[CarListing]) -> dict[int, dict]:
        async with limit:
            found = await one(offset, chunk)
            if found:
                return found
            # A chunk covering none of its listings means the model answered
            # a bare "[]". The prompt now forbids that explicitly — it used to
            # happen on roughly one chunk per run, because "use [] when there
            # is nothing" was being read as applying to the whole array rather
            # than to the inner lists — but it still slips out occasionally,
            # and a silent empty is indistinguishable from "this car had
            # nothing worth noting". One retry at a higher temperature breaks
            # the degenerate answer.
            log.info("assessor chunk at offset %s came back empty; retrying", offset)
            return await one(offset, chunk, temperature=0.5)

    batches = await asyncio.gather(
        *(bounded(i * CHUNK, chunk) for i, chunk in enumerate(chunks)),
        return_exceptions=True)

    merged: dict[int, dict] = {}
    for batch in batches:
        if isinstance(batch, dict):
            merged.update(batch)
        else:
            log.warning("assessor agent failed: %s", batch)
    return merged


def _strings(value, limit: int = 6) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip()[:40] for v in value if str(v).strip()][:limit]


# ── role 3: write the opening ───────────────────────────────────────────
_NARRATE_SYSTEM = (
    "You are a used-car buying advisor writing the opening of a market report "
    "for one shopper. You are given computed facts as JSON.\n"
    "Write 2 short paragraphs of plain prose, no headings, no bullet lists, no "
    "tables — a table follows your text, so do not duplicate it.\n"
    "Cover: what the market looks like right now, and the real tradeoff the "
    "buyer faces between price, kilometres and distance. Name at most three "
    "specific cars, by price and trim.\n"
    "Hard rules: use ONLY figures present in the JSON. You may state the "
    "difference between two figures that are both given — one car costing "
    "$4,697 more than another is a fair thing to say. Do not estimate, "
    "extrapolate, round, or introduce a figure that is not there. If the "
    "fit quality (r_squared) is below 0.4, say the mileage-price relationship is "
    "loose. Do not invent features, history or condition. Be direct and "
    "concrete; no sales language, no hedging filler."
)


def _facts(result: SwarmResult, analysis: Analysis,
           assessments: dict[int, dict] | None = None) -> dict:
    """The compact, already-computed digest the writer is allowed to use."""
    def brief(item: CarListing | None) -> dict | None:
        if item is None:
            return None
        return {"price": float(item.price) if item.price else None,
                "odometer_km": item.mileage_km, "trim": item.trim,
                "city": item.city, "distance_km": (round(item.distance_km, 1)
                                                   if item.distance_km is not None else None),
                "seller": item.seller_name, "seller_type": item.seller_type,
                "percent_vs_expected": (round(item.deal_score, 1)
                                        if item.deal_score is not None else None)}

    frontier = []
    for item in analysis.frontier[:8]:
        entry = brief(item)
        if entry:
            frontier.append(entry)

    notes: list[str] = []
    for data in (assessments or {}).values():
        notes.extend(data.get("flags") or [])

    return {
        "query": result.query.as_dict(),
        "cars_found": len(result.listings),
        "market": analysis.stats.as_dict(),
        "price_curve": analysis.curve.as_dict() if analysis.curve else None,
        "dealer_market": analysis.dealer_stats.as_dict() if analysis.dealer_stats else None,
        "private_market": analysis.private_stats.as_dict() if analysis.private_stats else None,
        "salvage_auction_floor": analysis.auction_floor,
        "undominated_choices": frontier,
        "picks": {"best_value": brief(analysis.best_value),
                  "cheapest": brief(analysis.cheapest),
                  "lowest_odometer": brief(analysis.lowest_mileage),
                  "closest": brief(analysis.closest)},
        "flags_seen": sorted(set(notes))[:10],
        "sources_that_failed": [c.source for c in result.coverage if c.status not in ("ok", "skipped")],
    }


async def narrate(result: SwarmResult, analysis: Analysis,
                  assessments: dict[int, dict] | None = None) -> str | None:
    """The prose opening, or None when no model is available."""
    if not llm.available() or not result.listings:
        return None
    import json
    return await llm.chat(
        [{"role": "system", "content": _NARRATE_SYSTEM},
         {"role": "user", "content": json.dumps(_facts(result, analysis, assessments),
                                                ensure_ascii=False, default=str)}],
        temperature=0.3, max_tokens=NARRATE_TOKENS)
