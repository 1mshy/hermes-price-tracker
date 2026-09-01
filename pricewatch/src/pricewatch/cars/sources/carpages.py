"""Carpages.ca — a second dealer aggregator, for the inventory AutoTrader misses.

Publishes one JSON-LD `Product`/`Car` node whose `offers` array is the whole
result page: title, price, and the listing URL. That is less than AutoTrader
gives us, but the title carries the year and trim by convention, and the URL
path carries the province and city (`/used-cars/quebec/saint-hubert/2024-audi-a3-…`),
which is enough to place and rank the car.
"""
from __future__ import annotations

import re

from lxml import html as lxml_html

from ...extract import _iter_jsonld, _types
from ...money import parse_price
from .. import taxonomy
from ..geo import PROVINCES, city_distance
from ..listing import RETAIL, SALVAGE, USED, CarListing
from ..spec import CarQuery
from .base import CarSource

BASE = "https://www.carpages.ca"
_PROVINCE_SLUGS = {name.lower().replace(" ", "-"): code for code, name in PROVINCES.items()}
_PROVINCE_SLUGS["quebec"] = "QC"
_PROVINCE_SLUGS["newfoundland-and-labrador"] = "NL"

# /used-cars/{province}/{city}/{year}-{make}-{model}-{id}/
_PATH_RE = re.compile(r"/used-cars/([a-z\-]+)/([a-z0-9\-]+)/", re.I)


class CarpagesSource(CarSource):
    key = "carpages"
    label = "Carpages.ca"
    channel = RETAIL

    #: 50 offers per page; three pages covers the national inventory of all but
    #: the highest-volume models without hammering the site.
    max_pages = 3

    def build_url(self, query: CarQuery, page: int = 1) -> str:
        """The make/model path, unscoped by geography — on purpose.

        Carpages 404s on a province-only path and its city path returns
        out-of-province cars anyway, so its location filtering buys nothing.
        The national listing has the best recall and the province and city are
        recoverable from each offer's own URL, which is what the swarm ranks on.
        """
        make = taxonomy.normalise_make(query.make)
        model = taxonomy.model_slug(query.model)
        parts = [BASE, "used-cars"]
        if make:
            parts.append(make)
        if model:
            parts.append(model)
        url = "/".join(parts) + "/"
        return f"{url}?p={page}" if page > 1 else url

    async def search(self, query: CarQuery) -> list[CarListing]:
        from ...fetch import Blocked, fetcher
        found: list[CarListing] = []
        seen: set[str] = set()
        for number in range(1, self.max_pages + 1):
            page = await fetcher.get(self.build_url(query, number))
            if page.looks_blocked:
                if number == 1:
                    raise Blocked(f"carpages.ca returned a challenge (HTTP {page.status})")
                break
            batch = [item for item in parse_results(page.text, query, source=self.key)
                     if item.url not in seen]
            if not batch:
                break
            seen.update(item.url for item in batch)
            found.extend(batch)
            if len(found) >= query.limit_per_source:
                break
        return found


def _place_from_url(url: str) -> tuple[str | None, str | None]:
    match = _PATH_RE.search(url)
    if not match:
        return None, None
    province = _PROVINCE_SLUGS.get(match.group(1).lower())
    city = match.group(2).replace("-", " ").title()
    return city, province


def parse_results(html: str, query: CarQuery, *, source: str = "carpages") -> list[CarListing]:
    try:
        doc = lxml_html.fromstring(html)
    except Exception:
        return []

    out: list[CarListing] = []
    seen: set[str] = set()
    for node in _iter_jsonld(doc):
        if not ({"car", "product", "vehicle"} & _types(node)):
            continue
        offers = node.get("offers")
        if isinstance(offers, dict):
            offers = [offers]
        if not isinstance(offers, list):
            continue
        for offer in offers:
            if not isinstance(offer, dict):
                continue
            listing = _listing_from_offer(offer, query, source)
            if listing is not None and listing.url not in seen:
                seen.add(listing.url)
                out.append(listing)
    return out


def _listing_from_offer(offer: dict, query: CarQuery, source: str) -> CarListing | None:
    url = str(offer.get("url") or "").strip()
    if not url:
        return None
    if url.startswith("/"):
        url = BASE + url

    title = str(offer.get("itemOffered") or offer.get("name") or "").strip()
    # The title omits the year on this site; the URL slug always carries it.
    year = taxonomy.parse_year(url) or taxonomy.parse_year(title)
    city, region = _place_from_url(url)

    make, model = None, None
    slug = re.search(r"/(\d{4})-([a-z0-9\-]+?)-(\d+)/?$", url)
    if slug:
        words = slug.group(2).split("-")
        if words:
            make = words[0]
            model = "-".join(words[1:]) or None

    condition_raw = str(offer.get("itemCondition") or "").lower()
    salvage = taxonomy.looks_salvage(title)

    listing = CarListing(
        source=source,
        url=url,
        title=f"{year} {title}".strip() if year and not taxonomy.parse_year(title) else (title or url),
        price=parse_price(offer.get("price")),
        currency=str(offer.get("priceCurrency") or "CAD").upper(),
        year=year,
        make=make,
        model=model,
        trim=taxonomy.extract_trim(title, year, make, model),
        mileage_km=taxonomy.parse_mileage_km(title),
        transmission=taxonomy.parse_transmission(title),
        drivetrain=taxonomy.parse_drivetrain(title),
        fuel=taxonomy.parse_fuel(title),
        condition=(SALVAGE if salvage else ("new" if "new" in condition_raw else USED)),
        channel=RETAIL,
        seller_type="dealer",
        city=city,
        region=region,
    )
    listing.distance_km = city_distance(query.place, city, region)
    return listing
