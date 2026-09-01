"""Facebook Marketplace — the private-seller side of the market.

Worth the trouble because it is the one place the aggregators do not reach:
owner-to-owner sales, priced several thousand below dealer retail because there
is no reconditioning, warranty or overhead in the number. A "best price" report
that skips Marketplace overstates the floor.

Marketplace has no public API and no JSON-LD. It does, however, ship its
GraphQL result inline in the page, so the listing objects are recoverable
without an account by scanning for them and brace-matching each one out of the
surrounding bootloader payload. That is a scrape and it is fragile by nature —
Facebook can change the shape whenever it likes — so this source is written to
return nothing rather than guess, and the swarm reports it as empty instead of
pretending the market has no private sellers.

Logged out, Facebook returns roughly the first screen of results and no
pagination. Treat its output as a sample of the private market, not a census.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from decimal import Decimal

from ...money import parse_price
from .. import taxonomy
from ..geo import city_distance
from ..listing import PRIVATE, SALVAGE, USED, CarListing
from ..spec import CarQuery
from .base import CarSource

log = logging.getLogger(__name__)

BASE = "https://www.facebook.com"
_MARKER = '"__typename":"GroupCommerceProductItem"'


class FacebookSource(CarSource):
    key = "facebook"
    label = "Facebook Marketplace"
    channel = PRIVATE

    def build_url(self, query: CarQuery) -> str:
        years = query.years
        terms = " ".join(filter(None, [
            str(years[0]) if len(years) == 1 else "",
            query.make, query.model,
        ])).strip()
        from urllib.parse import quote
        where = query.place.slug
        if where:
            return f"{BASE}/marketplace/{where}/search?query={quote(terms)}"
        return f"{BASE}/marketplace/category/search/?query={quote(terms)}"

    async def search(self, query: CarQuery) -> list[CarListing]:
        from ...fetch import fetcher
        page = await fetcher.get(self.build_url(query))
        listings = parse_results(page.text, query, source=self.key)
        if not listings and query.place.slug:
            # The city slug is a guess; Facebook 200s on an unknown one and
            # serves an empty shell. Fall back to the unscoped search, which
            # still returns local results and gets distance-filtered anyway.
            from urllib.parse import quote
            years = query.years
            terms = " ".join(filter(None, [
                str(years[0]) if len(years) == 1 else "", query.make, query.model])).strip()
            page = await fetcher.get(f"{BASE}/marketplace/category/search/?query={quote(terms)}")
            listings = parse_results(page.text, query, source=self.key)
        return listings


def _iter_objects(html: str, marker: str = _MARKER, limit: int = 200):
    """Yield each JSON object in `html` that contains `marker`.

    Facebook's payload is one enormous line, so this walks outward from each
    marker: back to the object's opening brace, then forward with a depth
    counter that respects string literals and escapes.
    """
    for found in re.finditer(re.escape(marker), html):
        if limit <= 0:
            return
        start = html.rfind("{", max(0, found.start() - 400), found.start() + 1)
        if start < 0:
            continue
        depth, index, length = 0, start, len(html)
        in_string = False
        while index < length and index - start < 60_000:
            char = html[index]
            if in_string:
                if char == "\\":
                    index += 2
                    continue
                if char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        yield json.loads(html[start:index + 1])
                        limit -= 1
                    except Exception:
                        pass
                    break
            index += 1


def parse_results(html: str, query: CarQuery, *, source: str = "facebook") -> list[CarListing]:
    out: list[CarListing] = []
    seen: set[str] = set()
    for node in _iter_objects(html or ""):
        listing = _listing_from_node(node, query, source)
        if listing is not None and listing.url not in seen:
            seen.add(listing.url)
            out.append(listing)
    return out


def _listing_from_node(node: dict, query: CarQuery, source: str) -> CarListing | None:
    listing_id = node.get("id")
    title = str(node.get("marketplace_listing_title") or "").strip()
    if not listing_id or not title:
        return None
    if node.get("is_sold") or node.get("is_hidden"):
        return None

    price_node = node.get("listing_price") or {}
    price = parse_price(price_node.get("amount"))
    if price is None:
        price = parse_price(price_node.get("formatted_amount"))

    geocode = ((node.get("location") or {}).get("reverse_geocode") or {})
    city = geocode.get("city") or None
    region = geocode.get("state") or None

    # Sellers write "2022 Audi A3 Komfort" or just "A3 8Y(2022)" — the year can
    # be anywhere, and `custom_title` sometimes carries the odometer.
    blob = f"{title} {node.get('custom_title') or ''}"
    year = taxonomy.parse_year(blob)

    make, model = None, None
    lowered = title.lower()
    for candidate in (query.make, taxonomy.normalise_make(query.make)):
        if candidate and re.search(rf"(?<![a-z]){re.escape(candidate.lower())}(?![a-z])", lowered):
            make = query.make
            break
    if re.search(rf"(?<![a-z0-9]){re.escape(query.model.lower())}(?![a-z0-9])", lowered):
        model = query.model

    created = node.get("creation_time")
    listed_at = None
    if isinstance(created, (int, float)) and created > 0:
        try:
            listed_at = dt.datetime.fromtimestamp(created, tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            listed_at = None

    photo = (((node.get("primary_listing_photo") or {}).get("image") or {}).get("uri"))

    listing = CarListing(
        source=source,
        url=f"{BASE}/marketplace/item/{listing_id}",
        title=title,
        price=price,
        currency="CAD",
        year=year,
        make=make,
        model=model,
        trim=taxonomy.extract_trim(title, year, make, model),
        mileage_km=taxonomy.parse_mileage_km(blob),
        transmission=taxonomy.parse_transmission(blob),
        drivetrain=taxonomy.parse_drivetrain(blob),
        fuel=taxonomy.parse_fuel(blob),
        condition=SALVAGE if taxonomy.looks_salvage(blob) else USED,
        channel=PRIVATE,
        seller_type="private",
        city=city,
        region=region,
        photos=[photo] if photo else [],
        extra={k: v for k, v in {
            "listed_at": listed_at.isoformat() if listed_at else None,
            "delivery": ",".join(node.get("delivery_types") or []) or None,
        }.items() if v},
    )
    listing.distance_km = city_distance(query.place, city, region)
    return listing
