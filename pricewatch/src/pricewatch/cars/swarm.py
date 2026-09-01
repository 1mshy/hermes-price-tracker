"""The coordinator: send every scout at once, then reconcile what they bring back.

The parallel fan-out is the cheap part. The work that makes the answer usable
is what happens afterwards:

* **Distance.** No source filters by radius honestly, so every listing is
  placed on the map and re-cut locally.
* **The query's own predicate.** Kijiji returns Q3s for an A3 search and Copart
  matches on the make alone; the sites' filtering is a hint, not a contract.
* **Deduplication.** The same car is on AutoTrader, Kijiji and Marketplace at
  once. Reporting it three times would triple-count the market and make a
  common car look like a glut. Merged records keep every URL, so the buyer can
  still see it is cross-posted — which is itself a negotiating fact.
* **Coverage.** Which scouts failed is part of the answer, not a log line.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from . import geo
from .listing import AUCTION, OK, CarListing, SourceOutcome
from .sources import base
from .sources.autotrader import AutoTraderSource
from .sources.carpages import CarpagesSource
from .sources.copart import CopartSource
from .sources.facebook import FacebookSource
from .sources.kijiji import KijijiSource
from .spec import CarQuery

log = logging.getLogger(__name__)

#: Every scout in the swarm, in the order their results are preferred when the
#: same car turns up twice. Richer sources lead: Kijiji publishes a VIN and
#: Kijiji's own price rating, AutoTrader has trim and dealer identity.
SOURCES: tuple[base.CarSource, ...] = (
    KijijiSource(),
    AutoTraderSource(),
    CarpagesSource(),
    FacebookSource(),
    CopartSource(),
)

SOURCE_BY_KEY = {s.key: s for s in SOURCES}


@dataclass
class SwarmResult:
    query: CarQuery
    listings: list[CarListing] = field(default_factory=list)
    coverage: list[SourceOutcome] = field(default_factory=list)
    rejected: int = 0
    duplicates_merged: int = 0
    elapsed_s: float = 0.0

    @property
    def retail(self) -> list[CarListing]:
        """The cars you can actually go and buy at a stated price."""
        return [x for x in self.listings if x.channel != AUCTION and x.ok]

    @property
    def auctions(self) -> list[CarListing]:
        return [x for x in self.listings if x.channel == AUCTION]

    def coverage_note(self) -> str | None:
        """A plain sentence about what the swarm could not see."""
        missed = [c for c in self.coverage if c.status != OK]
        if not missed:
            return None
        parts = [f"{c.source} ({c.status}{': ' + c.note if c.note else ''})" for c in missed]
        return "not counted in this report: " + "; ".join(parts)

    def as_dict(self) -> dict:
        return {
            "query": self.query.as_dict(),
            "found": len(self.listings),
            "retail_count": len(self.retail),
            "auction_count": len(self.auctions),
            "filtered_out": self.rejected,
            "duplicates_merged": self.duplicates_merged,
            "elapsed_s": round(self.elapsed_s, 2),
            "coverage": [c.as_dict() for c in self.coverage],
            "coverage_note": self.coverage_note(),
            "listings": [x.as_dict() for x in self.listings],
        }


async def gather(query: CarQuery, *, sources: list[str] | None = None,
                 timeout: float = 45.0) -> SwarmResult:
    """Run the swarm and return a reconciled, ranked set of listings."""
    started = time.monotonic()
    chosen = [SOURCE_BY_KEY[k] for k in sources if k in SOURCE_BY_KEY] if sources else list(SOURCES)
    usable = [s for s in chosen if s.supports(query)]

    outcomes = list(await asyncio.gather(
        *(base.run(source, query, timeout=timeout) for source in usable)))

    # Sources the query ruled out still belong in the coverage report — a
    # silent omission reads as "there were none".
    for source in chosen:
        if source not in usable:
            outcomes.append(SourceOutcome(
                source=source.key, status="skipped",
                note="not applicable to this query (auctions excluded)"
                     if source.channel == AUCTION else "does not cover this country"))

    everything = [item for outcome in outcomes for item in outcome.listings]
    await geo.annotate_distances(query.place, everything)

    kept: list[CarListing] = []
    rejected = 0
    for item in everything:
        keep, _ = query.matches(item)
        if keep:
            kept.append(item)
        else:
            rejected += 1

    merged, duplicates = deduplicate(kept)
    merged.sort(key=_rank_key)

    return SwarmResult(query=query, listings=merged, coverage=outcomes,
                       rejected=rejected, duplicates_merged=duplicates,
                       elapsed_s=time.monotonic() - started)


def _source_rank(listing: CarListing) -> int:
    order = {s.key: i for i, s in enumerate(SOURCES)}
    return order.get(listing.source, len(order))


def _completeness(listing: CarListing) -> int:
    """How much this record actually tells us, for picking a survivor."""
    return sum(1 for value in (listing.vin, listing.mileage_km, listing.trim,
                               listing.price, listing.transmission, listing.drivetrain,
                               listing.seller_name, listing.city) if value)


def _keys(listing: CarListing) -> list[str]:
    """Every identity this record could be recognised by.

    A listing carries more than one because the sources do not agree on what
    they publish: Kijiji gives a VIN, AutoTrader does not. Keying only on the
    VIN would mean those two never collide and the same car is counted twice.
    """
    keys = [listing.dedupe_key()]
    if listing.vin and len(listing.vin) >= 11:
        # the signature the VIN-less sources will produce for this same car
        price = int(listing.price) if listing.price is not None else -1
        km = round(listing.mileage_km / 500) if listing.mileage_km else -1
        model = re.sub(r"[^a-z0-9]", "", (listing.model or "").lower())
        keys.append(f"sig:{listing.year}:{model}:{price}:{km}")
    return keys


def _loose_key(listing: CarListing) -> str:
    model = re.sub(r"[^a-z0-9]", "", (listing.model or "").lower())
    price = int(listing.price) if listing.price is not None else -1
    return f"loose:{listing.year}:{model}:{price}"


def _trim_tokens(listing: CarListing) -> set[str]:
    """Trim words that actually name a version, ignoring feature spam."""
    noise = {"quattro", "awd", "fwd", "tfsi", "sedan", "hatchback", "sportback",
             "cuir", "toit", "ouvrant", "carplay", "navigation", "mags", "40",
             "45", "2.0t", "s", "line", "7sp", "tronic", "auto", "automatique"}
    words = re.findall(r"[a-z]+", (listing.trim or "").lower())
    return {w for w in words if w not in noise and len(w) > 2}


def _trims_compatible(left: list[CarListing], right: list[CarListing]) -> bool:
    a = set().union(*(_trim_tokens(x) for x in left)) if left else set()
    b = set().union(*(_trim_tokens(x) for x in right)) if right else set()
    if not a or not b:
        return True                        # nothing stated: no evidence against
    return bool(a & b)


def deduplicate(listings: list[CarListing]) -> tuple[list[CarListing], int]:
    """Collapse the same car seen on several sites into one record.

    Two passes, deliberately in this order:

    1. **Certain matches** — same VIN, or same year/model/price/odometer. Two
       cars agreeing on all four are one car.
    2. **Probable matches** — a record with no odometer at all (Marketplace
       rarely states one) is attached to a priced group it agrees with, but
       only when exactly one group is a candidate. Two different A3s really do
       sit at $26,995 in this market, so an ambiguous match is left alone
       rather than guessed; over-merging would hide a genuine second car.

    The survivor is the most complete record, not the first seen; the others'
    URLs are kept so the buyer can see the car is cross-posted, which is itself
    a negotiating fact.
    """
    parent: dict[str, str] = {}

    def find(key: str) -> str:
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for item in listings:
        keys = _keys(item)
        for key in keys[1:]:
            union(keys[0], key)

    groups: dict[str, list[CarListing]] = {}
    for item in listings:
        groups.setdefault(find(_keys(item)[0]), []).append(item)

    # Pass 2: attach odometer-less records to an unambiguous priced group.
    by_loose: dict[str, list[str]] = {}
    for root, group in groups.items():
        if any(x.mileage_km for x in group):
            by_loose.setdefault(_loose_key(group[0]), []).append(root)

    for root in list(groups):
        group = groups.get(root)
        if not group or any(x.mileage_km for x in group):
            continue
        candidates = by_loose.get(_loose_key(group[0]), [])
        if len(candidates) != 1:
            continue                       # ambiguous — two real cars share this price
        target = candidates[0]
        if target == root or target not in groups:
            continue
        near = [x.distance_km for x in group + groups[target] if x.distance_km is not None]
        if near and max(near) - min(near) > 40:
            continue                       # same price, different town: different car
        if not _trims_compatible(group, groups[target]):
            continue                       # a Komfort and a Progressiv at one price are two cars
        groups[target].extend(group)
        del groups[root]

    survivors: list[CarListing] = []
    duplicates = 0
    for group in groups.values():
        if len(group) == 1:
            survivors.append(group[0])
            continue
        duplicates += len(group) - 1
        group.sort(key=lambda x: (-_completeness(x), _source_rank(x)))
        winner, others = group[0], group[1:]
        winner.extra = dict(winner.extra)
        winner.extra["also_on"] = [
            {"source": o.source, "url": o.url,
             **({"price": float(o.price)} if o.price is not None
                and o.price != winner.price else {})}
            for o in others
        ]
        for other in others:
            for attribute in ("vin", "mileage_km", "trim", "transmission", "drivetrain",
                              "fuel", "body", "exterior_color", "seller_name",
                              "seller_type", "city", "region", "price_rating",
                              "latitude", "longitude", "distance_km"):
                if getattr(winner, attribute, None) in (None, "") and getattr(other, attribute, None):
                    setattr(winner, attribute, getattr(other, attribute))
        survivors.append(winner)
    return survivors, duplicates


def _rank_key(listing: CarListing):
    """Cheapest real asking price first; auctions last, they are not comparable."""
    price = listing.effective_price
    return (
        1 if listing.channel == AUCTION else 0,
        float(price) if price is not None else float("inf"),
        listing.mileage_km if listing.mileage_km is not None else 10**9,
    )
