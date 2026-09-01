"""Kijiji Autos — the private-seller half of the market, plus dealer ads.

Richest source by some distance: its Next.js page ships an Apollo cache whose
`AutosListing` entities carry VIN, trim, odometer, drivetrain, seller type,
exact coordinates, and Kijiji's own price rating. Reading that cache is both
easier and more reliable than the DOM, and the VIN is what lets the swarm
recognise the same car on AutoTrader.

Two Kijiji quirks shape this adapter. Its keyword search is loose — ask for an
A3 and it returns Q3s — so we use the structured attribute filters
(`carmake`/`carmodel`/`caryear`) instead. And its geography is a numeric tree,
so the city name is resolved against the tree the site itself publishes rather
than a hardcoded table that would rot.
"""
from __future__ import annotations

import json
import logging
import re
from decimal import Decimal

from ...fetch import fetcher
from .. import taxonomy
from ..geo import Place, distance_to
from ..listing import AUCTION, PRIVATE, RETAIL, SALVAGE, USED, CarListing
from ..spec import CarQuery
from .base import CarSource

log = logging.getLogger(__name__)

BASE = "https://www.kijiji.ca"
CARS_CATEGORY = 174
LOCATIONS_URL = f"{BASE}/j-locations.json"

# Verified province ids; the fallback when the city is not in the tree.
PROVINCE_IDS = {
    "QC": 9001, "NS": 9002, "AB": 9003, "ON": 9004, "NB": 9005, "MB": 9006,
    "BC": 9007, "NL": 9008, "SK": 9009, "NT": 9010, "NU": 9010, "YT": 9010,
    "PE": 9002,
}
CANADA_ID = 0

_TREE_CACHE: dict | None = None


async def _location_tree() -> dict | None:
    """Kijiji's own location tree, fetched once per process.

    Served as a JavaScript assignment rather than JSON, so the `var x = ` head
    is trimmed before parsing.
    """
    global _TREE_CACHE
    if _TREE_CACHE is not None:
        return _TREE_CACHE or None
    try:
        page = await fetcher.get(LOCATIONS_URL)
        text = (page.text or "").strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("unrecognised locations payload")
        _TREE_CACHE = json.loads(text[start:end + 1])
    except Exception as exc:                           # noqa: BLE001 — fall back to province
        log.debug("kijiji location tree unavailable: %s", exc)
        _TREE_CACHE = {}
    return _TREE_CACHE or None


def _norm_place(text: str) -> str:
    text = text.lower()
    for accented, plain in (("é", "e"), ("è", "e"), ("ê", "e"), ("à", "a"),
                            ("â", "a"), ("î", "i"), ("ô", "o"), ("û", "u"), ("ç", "c")):
        text = text.replace(accented, plain)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def find_location_id(tree: dict | None, city: str) -> int | None:
    """Walk the tree for a city, in either official language.

    Prefers the deepest exact name match; falls back to a region whose name
    contains the city ("Laval / North Shore" for "Laval"), which is the right
    answer anyway — nobody buying in Laval refuses a car in Sainte-Thérèse.
    """
    if not tree or not city:
        return None
    wanted = _norm_place(city)
    exact: list[tuple[int, int]] = []
    partial: list[tuple[int, int]] = []

    def walk(node: dict, depth: int = 0) -> None:
        if depth > 6 or not isinstance(node, dict):
            return
        names = [node.get("nameEn"), node.get("nameFr")]
        node_id = node.get("id")
        if isinstance(node_id, int):
            for name in names:
                if not name:
                    continue
                normalised = _norm_place(str(name))
                if normalised == wanted:
                    exact.append((depth, node_id))
                elif wanted and wanted in normalised.split(" / ") + normalised.split():
                    partial.append((depth, node_id))
        for child in (node.get("children") or []):
            walk(child, depth + 1)

    walk(tree)
    if exact:
        return min(exact)[1]
    if partial:
        return min(partial)[1]
    return None


async def location_id_for(place: Place, radius_km: float = 150.0) -> int:
    """Best Kijiji geography for a place — deliberately wider than the radius.

    Measured against the live site: Kijiji's leaf regions are far narrower than
    a shopper's idea of "near me". "Laval / North Shore" held no 2022 A3 at all
    while the province held three, every one of them inside 150 km of Laval. So
    the scout searches the province and lets the swarm cut on true distance;
    the narrow node would simply have missed cars that were 20 minutes away.
    """
    if radius_km > 400:
        return CANADA_ID
    if place.region:
        return PROVINCE_IDS.get(place.region.upper(), CANADA_ID)
    if place.city:
        tree = await _location_tree()
        found = find_location_id(tree, place.city)
        if found is not None:
            return found
    return CANADA_ID


class KijijiSource(CarSource):
    key = "kijiji"
    label = "Kijiji Autos"
    channel = RETAIL          # carries both; each listing decides for itself

    async def build_url(self, query: CarQuery, page: int = 1) -> str:
        location = await location_id_for(query.place, query.radius_km)
        params = []
        make = taxonomy.normalise_make(query.make)
        if make:
            params.append(f"carmake={make}")
        model = taxonomy.model_slug(query.model)
        if model:
            params.append(f"carmodel={model}")
        years = query.years
        if years:
            params.append(f"caryear={years[0]}__{years[-1]}")
        if query.max_mileage_km:
            params.append(f"carmileageinkms=0__{query.max_mileage_km}")
        path = f"{BASE}/b-cars-trucks/c{CARS_CATEGORY}l{location}"
        if page > 1:
            path += f"/page-{page}"
        return path + ("?" + "&".join(params) if params else "")

    async def keyword_url(self, query: CarQuery) -> str:
        """The loose search, used only when the strict one finds nothing."""
        location = await location_id_for(query.place, query.radius_km)
        terms = "-".join(filter(None, [taxonomy.normalise_make(query.make),
                                       taxonomy.model_slug(query.model)]))
        return f"{BASE}/b-cars-trucks/{terms}/k0c{CARS_CATEGORY}l{location}"

    async def search(self, query: CarQuery) -> list[CarListing]:
        from ...fetch import Blocked
        page = await fetcher.get(await self.build_url(query))
        if page.looks_blocked:
            raise Blocked(f"kijiji.ca returned a challenge (HTTP {page.status})")
        found = parse_results(page.text, query, source=self.key)
        if found:
            return found

        # Kijiji's model filter is a closed vocabulary, and a model whose name
        # has two words is not in it: `carmodel=golf-gti` and `carmodel=model-3`
        # both match nothing at all, silently, while the cars are plainly
        # listed. Fall back to the keyword search, which is loose enough to
        # find them — the swarm re-filters everything locally anyway, so the
        # extra noise costs nothing and the alternative is missing the model
        # entirely.
        page = await fetcher.get(await self.keyword_url(query))
        if page.looks_blocked:
            return []
        return parse_results(page.text, query, source=self.key)


def _apollo_state(html: str) -> dict:
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except Exception:
        return {}
    return (data.get("props", {}).get("pageProps", {}).get("__APOLLO_STATE__") or {})


def parse_results(html: str, query: CarQuery, *, source: str = "kijiji") -> list[CarListing]:
    state = _apollo_state(html)
    out: list[CarListing] = []
    for key, node in state.items():
        if not key.startswith("AutosListing:") or not isinstance(node, dict):
            continue
        listing = _listing_from_node(node, query, source)
        if listing is not None:
            out.append(listing)
    return out


def _attrs(node: dict) -> dict[str, str]:
    values: dict[str, str] = {}
    for attribute in (node.get("attributes") or {}).get("all") or []:
        name = attribute.get("canonicalName")
        raw = attribute.get("canonicalValues") or []
        if name and raw:
            values[str(name)] = str(raw[0])
    return values


def _listing_from_node(node: dict, query: CarQuery, source: str) -> CarListing | None:
    url = node.get("url")
    if not url:
        return None
    attributes = _attrs(node)
    title = str(node.get("title") or "").strip()
    description = str(node.get("description") or "")
    blob = f"{title} {description}"

    # Kijiji quotes money in cents.
    price = None
    price_node = node.get("price") or {}
    amount = price_node.get("amount")
    if isinstance(amount, (int, float)) and amount > 0:
        price = Decimal(str(amount)) / 100

    year = None
    if attributes.get("caryear", "").isdigit():
        year = int(attributes["caryear"])
    year = year or taxonomy.parse_year(title)

    mileage = None
    if attributes.get("carmileageinkms", "").isdigit():
        mileage = int(attributes["carmileageinkms"])
    mileage = mileage or taxonomy.parse_mileage_km(blob)

    seller_type = {"delr": "dealer", "ownr": "private"}.get(attributes.get("forsaleby", ""))
    location = node.get("location") or {}
    coordinates = location.get("coordinates") or {}
    latitude = coordinates.get("latitude")
    longitude = coordinates.get("longitude")

    condition = USED
    if taxonomy.looks_salvage(blob):
        condition = SALVAGE
    elif attributes.get("vehicletype") == "new":
        condition = "new"

    listing = CarListing(
        source=source,
        url=str(url),
        title=title or str(url),
        price=price,
        currency="CAD",
        year=year,
        make=attributes.get("carmake") or None,
        model=attributes.get("carmodel") or None,
        trim=attributes.get("cartrim") or None,
        mileage_km=mileage,
        vin=(attributes.get("vin") or None),
        transmission=(taxonomy.parse_transmission(blob)
                      or {"1": "manual", "2": "automatic"}.get(attributes.get("cartransmission", ""))),
        drivetrain=(attributes.get("drivetrain") or taxonomy.parse_drivetrain(blob)),
        fuel=(attributes.get("carfueltype") or taxonomy.parse_fuel(blob)),
        body=attributes.get("carbodytype") or None,
        exterior_color=attributes.get("carcolor") or None,
        condition=condition,
        channel=PRIVATE if seller_type == "private" else RETAIL,
        # Only a real poster name. Falling back to the location put "City of
        # Montréal" in the dealer column and invented a dealership out of a
        # neighbourhood.
        seller_name=((node.get("posterInfo") or {}).get("name") or None),
        seller_type=seller_type,
        city=location.get("name") or None,
        region=query.place.region or None,
        latitude=latitude,
        longitude=longitude,
        price_rating=((price_node.get("classification") or {}).get("rating")
                      or attributes.get("pricerating") or None),
        photos=[p for p in (node.get("imageUrls") or []) if isinstance(p, str)][:1],
        extra={k: v for k, v in {
            "msrp_cents": price_node.get("msrp"),
            "surcharges": price_node.get("surcharges"),
            "stock": attributes.get("stock"),
        }.items() if v},
    )
    listing.distance_km = distance_to(query.place, latitude, longitude)
    return listing
