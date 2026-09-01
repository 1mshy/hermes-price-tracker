"""The one shape every car source normalises into.

Sources disagree about everything: Kijiji reports price in cents and mileage as
a string, AutoTrader publishes schema.org `Car` nodes, Copart hands back an
auction lot with two-letter field names. A report that compares them has to
compare like with like, so each source's adapter is responsible for filling in
this dataclass and nothing else downstream knows where a listing came from.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from decimal import Decimal

# Channel — how you would actually buy the car, which matters more for a
# tradeoff report than which website carried the ad. An auction lot at $2,300
# is not competing with a $28,000 dealer listing and must never be ranked
# against it as though it were.
RETAIL, PRIVATE, AUCTION = "retail", "private", "auction"

# Condition. "salvage" is its own thing rather than a flavour of used: those
# cars are branded/rebuilt and a naive cheapest-first list is dominated by them.
NEW, USED, SALVAGE = "new", "used", "salvage"


@dataclass
class CarListing:
    """One vehicle for sale, from any source."""

    source: str
    url: str
    title: str

    price: Decimal | None = None
    currency: str = "CAD"

    year: int | None = None
    make: str | None = None
    model: str | None = None
    trim: str | None = None

    mileage_km: int | None = None
    vin: str | None = None

    transmission: str | None = None
    drivetrain: str | None = None
    fuel: str | None = None
    body: str | None = None
    exterior_color: str | None = None
    doors: int | None = None

    condition: str = USED
    channel: str = RETAIL

    seller_name: str | None = None
    seller_type: str | None = None          # "dealer" | "private" | "auction"
    city: str | None = None
    region: str | None = None               # province/state code
    latitude: float | None = None
    longitude: float | None = None
    distance_km: float | None = None

    #: the source's own price verdict, where it publishes one (Kijiji's
    #: GOOD/FAIR/…). Kept separate from our computed deal score: it is their
    #: opinion about their own market, useful as corroboration, not as truth.
    price_rating: str | None = None

    # auction-only
    auction_ends_at: dt.datetime | None = None
    current_bid: Decimal | None = None

    photos: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    # filled in by the swarm, not by sources
    deal_score: float | None = None
    expected_price: Decimal | None = None

    @property
    def ok(self) -> bool:
        """Usable in a price comparison at all."""
        return self.price is not None and self.price > 0

    @property
    def effective_price(self) -> Decimal | None:
        """What it costs today — an auction lot's live bid is not its ask."""
        if self.channel == AUCTION and self.current_bid is not None:
            return self.current_bid
        return self.price

    def dedupe_key(self) -> str:
        """Identity across sources.

        A VIN is definitive and every serious source that has one publishes it.
        Without one, the same car syndicated to three sites still agrees on
        year, model, price and odometer, so those pinned together are a safe
        surrogate — mileage is bucketed because sites round it differently.
        """
        if self.vin and len(self.vin) >= 11:
            return f"vin:{self.vin.upper()}"
        price = int(self.price) if self.price is not None else -1
        model = re.sub(r"[^a-z0-9]", "", (self.model or "").lower())
        if not self.mileage_km:
            # No odometer means no evidence of identity. Treating "unknown" as
            # a value makes every same-priced car collapse into one — three
            # different A3s all listed at $26,995 became a single car. Stay
            # unique here and let the swarm's ambiguity-checked second pass
            # decide, or leave them apart.
            return f"solo:{self.source}:{self.url}"
        km = round(self.mileage_km / 500)
        return f"sig:{self.year}:{model}:{price}:{km}"

    def as_dict(self) -> dict:
        payload = {
            "source": self.source,
            "url": self.url,
            "title": self.title,
            "price": float(self.price) if self.price is not None else None,
            "currency": self.currency,
            "year": self.year,
            "make": self.make,
            "model": self.model,
            "trim": self.trim,
            "mileage_km": self.mileage_km,
            "vin": self.vin,
            "transmission": self.transmission,
            "drivetrain": self.drivetrain,
            "fuel": self.fuel,
            "body": self.body,
            "exterior_color": self.exterior_color,
            "condition": self.condition,
            "channel": self.channel,
            "seller_name": self.seller_name,
            "seller_type": self.seller_type,
            "city": self.city,
            "region": self.region,
            "distance_km": round(self.distance_km, 1) if self.distance_km is not None else None,
            "price_rating": self.price_rating,
        }
        if self.channel == AUCTION:
            payload["current_bid"] = float(self.current_bid) if self.current_bid is not None else None
            payload["auction_ends_at"] = (self.auction_ends_at.isoformat()
                                          if self.auction_ends_at else None)
        if self.deal_score is not None:
            payload["deal_score"] = round(self.deal_score, 1)
        if self.expected_price is not None:
            payload["expected_price"] = float(self.expected_price)
        if self.photos:
            payload["photo"] = self.photos[0]
        payload = {k: v for k, v in payload.items() if v is not None}
        # `extra` carries the things that decide whether a figure is quotable
        # at all — where else the car is listed, what the damage is, whether a
        # deal score was mileage-adjusted, what the seller claimed. Dropping it
        # here left every one of those out of the structured answer while the
        # rendered report still showed them.
        details = {k: v for k, v in self.extra.items() if v not in (None, "", [], {})}
        if details:
            payload["extra"] = details
        return payload


# Source status vocabulary, deliberately the same words verify.py already uses
# for stores, so "blocked" means the same thing everywhere in this codebase.
OK, BLOCKED, EMPTY, ERROR, NEEDS_KEY = "ok", "blocked", "empty", "error", "needs-key"


@dataclass
class SourceOutcome:
    """What one scout came back with — including how it failed.

    A swarm that silently drops a source that broke will happily report "the
    cheapest 2022 A3 near you is $28,000" when the site holding the $24,000 one
    timed out. Every run carries its own coverage report so the answer can say
    what it did not see.
    """

    source: str
    status: str = OK
    listings: list[CarListing] = field(default_factory=list)
    note: str | None = None
    elapsed_s: float = 0.0
    url: str | None = None

    def as_dict(self) -> dict:
        out = {"source": self.source, "status": self.status,
               "found": len(self.listings), "elapsed_s": round(self.elapsed_s, 2)}
        if self.note:
            out["note"] = self.note
        if self.url:
            out["url"] = self.url
        return out
