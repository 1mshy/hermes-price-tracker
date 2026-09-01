"""Rendering a car search as something a buyer can act on.

Deterministic and complete: this is both the answer when no model is available
and the evidence the model is handed when one is. Every row carries its source
and URL, because the whole point is that the reader can go and look.
"""
from __future__ import annotations

import datetime as dt

from .listing import AUCTION, CarListing
from .swarm import SwarmResult
from .valuation import Analysis


def _cell(text) -> str:
    """Make a value safe inside a markdown table.

    Dealers write trims like "40 Komfort | Cuir brun | Quattro", and an
    unescaped pipe silently splits the row into extra columns.
    """
    if text is None:
        return "—"
    return str(text).replace("|", "\\|").replace("\n", " ").strip() or "—"


def _money(value) -> str:
    if value is None:
        return "—"
    return f"${float(value):,.0f}"


def _km(value) -> str:
    return f"{value:,} km" if value else "not stated"


def _distance(listing: CarListing) -> str:
    if listing.distance_km is None:
        return "—"
    if listing.distance_km < 1:
        return "in town"
    return f"{listing.distance_km:.0f} km"


def _where(listing: CarListing) -> str:
    bits = [listing.city or "", listing.region or ""]
    return ", ".join(b for b in bits if b) or "—"


def _seller(listing: CarListing) -> str:
    if listing.seller_name:
        return listing.seller_name
    return {"private": "private seller", "dealer": "dealer",
            "auction": "auction"}.get(listing.seller_type or "", "—")


def _sources(listing: CarListing) -> str:
    also = listing.extra.get("also_on") or []
    names = [listing.source] + [a["source"] for a in also]
    return "+".join(dict.fromkeys(names))


def _score(listing: CarListing) -> str:
    if listing.deal_score is None:
        return "—"
    basis = listing.extra.get("score_basis")
    mark = "" if basis == "mileage-adjusted" else "*"
    return f"{listing.deal_score:+.1f}%{mark}"


def render(result: SwarmResult, analysis: Analysis, *,
           narrative: str | None = None, assessments: dict | None = None) -> str:
    query = result.query
    stats = analysis.stats
    lines: list[str] = []
    add = lines.append

    add(f"# {query.label}")
    add("")
    where = query.place.city or query.place.region_name or "your area"
    add(f"**{len(result.listings)} cars** within {query.radius_km:.0f} km of {where}, "
        f"from {sum(1 for c in result.coverage if c.status == 'ok')} live sources · "
        f"{dt.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    add("")

    if narrative:
        add(narrative.strip())
        add("")

    # ── the market ──────────────────────────────────────────────────────
    add("## The market")
    add("")
    if stats.count:
        add(f"- **Asking prices** {_money(stats.low)} – {_money(stats.high)}, "
            f"median **{_money(stats.median)}**"
            + (f" (middle half {_money(stats.p25)}–{_money(stats.p75)})" if stats.p25 else ""))
        if stats.median_mileage:
            add(f"- **Typical odometer** {_km(stats.median_mileage)}")
    else:
        add("- No priced retail listings matched.")

    if analysis.curve:
        curve = analysis.curve
        add(f"- **What kilometres cost here** about "
            f"{_money(abs(curve.slope) * 1000)} per 1,000 km "
            f"(fitted on {curve.points} cars, r²={curve.r_squared:.2f})")
        if curve.r_squared < 0.35:
            add("  - a loose fit: mileage explains only part of the spread, so treat "
                "the mileage-adjusted figures as a guide rather than a valuation")
    else:
        add("- Too few cars with a stated odometer to fit a local price curve; "
            "value is measured against the median instead.")

    dealer, private = analysis.dealer_stats, analysis.private_stats
    if dealer and dealer.count and private and private.count:
        add(f"- **Dealer vs private** dealers median {_money(dealer.median)} "
            f"({dealer.count} cars) · private sellers median {_money(private.median)} "
            f"({private.count} cars)")
    if analysis.auction_floor is not None:
        add(f"- **Salvage floor** damaged examples are bidding from "
            f"{_money(analysis.auction_floor)} at auction — the bottom of the market, "
            f"and not a car you can simply drive home")
    add("")

    # ── the shortlist ───────────────────────────────────────────────────
    if analysis.frontier:
        add("## Shortlist — the genuine tradeoffs")
        add("")
        add("Every car here is one that **nothing else beats on both price and "
            "kilometres**. Anything left off is worse than one of these on both "
            "counts at once, so no preference between money and mileage would pick it.")
        add("")
        add("| Price | Odometer | vs. expected | Trim | Where | Distance | Seller | Seen on |")
        add("|---|---|---|---|---|---|---|---|")
        for item in analysis.frontier:
            add(f"| [{_money(item.price)}]({item.url}) | {_km(item.mileage_km)} | "
                f"{_score(item)} | {_cell(item.trim)} | {_cell(_where(item))} | "
                f"{_distance(item)} | {_cell(_seller(item))} | {_sources(item)} |")
        add("")
        add("*vs. expected* compares the asking price with what the local curve "
            "predicts for that odometer. Positive is under the line.")
        add("")

    # ── what the sellers said ───────────────────────────────────────────
    if assessments:
        noted = [(i, d) for i, d in sorted(assessments.items())
                 if d.get("flags") or d.get("condition_notes") or d.get("equipment")]
        if noted:
            add("## What the sellers say")
            add("")
            add("Read off each listing, French ones included. These are the sellers' "
                "own claims, not an inspection — and a car with nothing listed here "
                "simply said nothing, which is not the same as having nothing to hide.")
            unread = len(result.listings) - len(assessments)
            if unread > 0:
                add("")
                add(f"({unread} of the {len(result.listings)} listings were not read — "
                    f"their absence below means nothing either way.)")
            add("")
            for index, data in noted:
                if index >= len(result.listings):
                    continue
                item = result.listings[index]
                parts = []
                if data.get("flags"):
                    parts.append("⚠️ " + "; ".join(data["flags"]))
                if data.get("condition_notes"):
                    parts.append("; ".join(data["condition_notes"]))
                if data.get("equipment"):
                    parts.append("kit: " + ", ".join(data["equipment"][:5]))
                add(f"- **{_money(item.price)}** "
                    f"[{item.trim or item.title[:40]}]({item.url}) — "
                    + " · ".join(parts))
            add("")

    # ── picks ───────────────────────────────────────────────────────────
    picks = [
        ("Best value for the kilometres", analysis.best_value),
        ("Cheapest", analysis.cheapest),
        ("Lowest odometer", analysis.lowest_mileage),
        ("Closest to you", analysis.closest),
    ]
    shown = [(label, item) for label, item in picks if item is not None]
    if shown:
        add("## Picks")
        add("")
        for label, item in shown:
            add(f"- **{label}** — [{_money(item.price)}, {_km(item.mileage_km)}, "
                f"{item.trim or 'trim not stated'}]({item.url}) "
                f"at {_seller(item)}, {_where(item)} ({_distance(item)})"
                f"{_pick_note(item, analysis)}")
        add("")

    # ── dealers ─────────────────────────────────────────────────────────
    if analysis.dealers:
        add("## Where the cars are")
        add("")
        add("| Seller | City | Units | Cheapest | Typical vs. market | Distance |")
        add("|---|---|---|---|---|---|")
        for posture in analysis.dealers:
            score = ("—" if posture.median_deal_score is None
                     else f"{posture.median_deal_score:+.1f}%")
            distance = ("—" if posture.distance_km is None
                        else f"{posture.distance_km:.0f} km")
            add(f"| {_cell(posture.name)} | {_cell(posture.city)} | {posture.units} | "
                f"{_money(posture.cheapest)} | {score} | {distance} |")
        add("")

    # ── everything ──────────────────────────────────────────────────────
    retail = result.retail
    if retail:
        add("## Every car found")
        add("")
        add("| Price | Odometer | vs. expected | Trim | Where | Distance | Seller | Seen on |")
        add("|---|---|---|---|---|---|---|---|")
        for item in retail:
            add(f"| [{_money(item.price)}]({item.url}) | {_km(item.mileage_km)} | "
                f"{_score(item)} | {_cell(item.trim)} | {_cell(_where(item))} | "
                f"{_distance(item)} | {_cell(_seller(item))} | {_sources(item)} |")
        add("")
        if any(x.extra.get("score_basis") == "median-only" for x in retail):
            add("\\* scored against the median only — this listing states no odometer, "
                "so it cannot be compared on the mileage curve.")
            add("")

    ignored = [x for x in result.listings if x.extra.get("not_counted")]
    if ignored:
        add("## Seen but not counted")
        add("")
        for item in ignored:
            add(f"- [{_money(item.price)} — {_cell(item.title)[:70]}]({item.url}) "
                f"({item.source}): {item.extra['not_counted']}")
        add("")

    auctions = result.auctions
    if auctions:
        add("## Salvage auction lots")
        add("")
        add("Damaged or written-off cars. Bids exclude buyer fees and taxes, and most "
            "need a rebuild inspection before they can be registered.")
        add("")
        add("| Current bid | Odometer | Damage | Where | Auction |")
        add("|---|---|---|---|---|")
        for item in auctions[:10]:
            add(f"| [{_money(item.current_bid)}]({item.url}) | {_km(item.mileage_km)} | "
                f"{_cell(item.extra.get('damage'))} | {_cell(_where(item))} | "
                f"{item.auction_ends_at.strftime('%Y-%m-%d') if item.auction_ends_at else '—'} |")
        add("")

    # ── coverage ────────────────────────────────────────────────────────
    add("## Coverage")
    add("")
    for outcome in sorted(result.coverage, key=lambda c: c.source):
        mark = {"ok": "✓", "empty": "·", "skipped": "·"}.get(outcome.status, "✗")
        detail = f" — {outcome.note}" if outcome.note else ""
        add(f"- {mark} **{outcome.source}** {outcome.status}, "
            f"{len(outcome.listings)} raw{detail}")
    add("")
    add(f"{result.rejected} listings were read and discarded for not matching "
        f"(wrong year, model, too far, or salvage); {result.duplicates_merged} "
        f"cross-postings were merged into the cars above.")
    if result.coverage_note():
        add("")
        add(f"**Caveat:** {result.coverage_note()}")
    return "\n".join(lines)


def _pick_note(item: CarListing, analysis: Analysis) -> str:
    if item.deal_score is None:
        return ""
    if item.extra.get("score_basis") != "mileage-adjusted":
        return " — no odometer stated, so this is measured against the median only"
    if item.deal_score >= 0:
        return f" — about {item.deal_score:.0f}% under what its odometer predicts"
    return f" — about {abs(item.deal_score):.0f}% over what its odometer predicts"
