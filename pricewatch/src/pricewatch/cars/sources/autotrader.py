"""AutoTrader.ca — the largest Canadian dealer inventory, read from its JSON-LD.

The search page publishes a `SearchResultsPage` whose `mainEntity` is an
`ItemList` of schema.org `Car` nodes carrying trim, odometer, fuel,
transmission, price and the selling dealer's city. That is a far better
contract than the DOM: it is what Google indexes, so the site has every
incentive to keep it stable, and it survives the front-end rewrites that break
CSS selectors.

Location lives in the path (`/my_2022/reg_qc/cit_laval`) rather than a query
string; the query-string forms redirect and silently drop the filter, which is
how you end up with Winnipeg cars in a Laval search.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal

from lxml import html as lxml_html

from ...extract import _iter_jsonld, _types
from ...money import parse_price
from ...fetch import fetcher
from .. import taxonomy
from ..geo import city_distance
from ..listing import RETAIL, SALVAGE, USED, CarListing
from ..spec import CarQuery
from .base import CarSource

BASE = "https://www.autotrader.ca"
PAGE_SIZE = 100


class AutoTraderSource(CarSource):
    key = "autotrader"
    label = "AutoTrader.ca"
    channel = RETAIL

    def build_url(self, query: CarQuery, page: int = 1) -> str:
        make = taxonomy.normalise_make(query.make)
        model = taxonomy.model_slug(query.model)
        path = [BASE, "cars"]
        if make:
            path.append(make)
        if model:
            path.append(model)

        years = query.years
        if len(years) == 1:
            path.append(f"my_{years[0]}")

        place = query.place
        if place.region:
            path.append(f"reg_{place.region.lower()}")
        if place.city and place.latitude is not None:
            path.append(f"cit_{place.slug}")

        url = "/".join(path)
        params = [f"rcp={PAGE_SIZE}"]
        if len(years) > 1:
            params.append(f"yRng={years[0]}%2C{years[-1]}")
        if page > 1:
            params.append(f"rcs={(page - 1) * PAGE_SIZE}")
        if place.postal:
            params.append(f"loc={place.postal}")
            params.append(f"prx={int(query.radius_km)}")
        return url + ("?" + "&".join(params) if params else "")

    async def search(self, query: CarQuery) -> list[CarListing]:
        url = self.build_url(query)
        page = await fetcher.get(url)
        if page.looks_blocked:
            from ...fetch import Blocked
            raise Blocked(f"autotrader.ca returned a challenge (HTTP {page.status})")
        return parse_results(page.text, query, source=self.key)


def parse_results(html: str, query: CarQuery, *, source: str = "autotrader") -> list[CarListing]:
    """Every `Car` node in the page's JSON-LD, normalised."""
    try:
        doc = lxml_html.fromstring(html)
    except Exception:
        return []

    out: list[CarListing] = []
    seen: set[str] = set()
    for node in _iter_jsonld(doc):
        kinds = _types(node)
        if not ({"car", "vehicle", "product"} & kinds):
            continue
        listing = _listing_from_node(node, query, source)
        if listing is not None and listing.url not in seen:
            seen.add(listing.url)
            out.append(listing)
    return out


def _listing_from_node(node: dict, query: CarQuery, source: str) -> CarListing | None:
    offer = node.get("offers")
    if isinstance(offer, list):
        offer = offer[0] if offer else None
    if not isinstance(offer, dict):
        return None

    url = str(offer.get("url") or node.get("@id") or "").split("#")[0]
    if not url:
        return None
    if url.startswith("/"):
        url = BASE + url

    price = parse_price(offer.get("price"))
    title = str(node.get("name") or "").strip()

    seller = offer.get("seller") if isinstance(offer.get("seller"), dict) else {}
    address = seller.get("address") if isinstance(seller.get("address"), dict) else {}
    city = address.get("addressLocality") or None
    region = address.get("addressRegion") or None

    odo = node.get("mileageFromOdometer")
    mileage = None
    if isinstance(odo, dict):
        value = odo.get("value")
        mileage = int(value) if isinstance(value, (int, float)) and value > 0 else None
        if mileage and str(odo.get("unitCode", "")).upper() in ("SMI", "MI"):
            mileage = int(round(mileage * taxonomy.MILES_TO_KM))
    elif odo:
        mileage = taxonomy.parse_mileage_km(str(odo))

    # The year is not a field here — it is in the URL slug and, for the
    # listings that carry one, the model-year breadcrumb. Fall back to the
    # title, then to the query when the page was year-scoped.
    year = taxonomy.parse_year(url) or taxonomy.parse_year(title)
    if year is None and len(query.years) == 1:
        year = query.years[0]

    condition = str(node.get("itemCondition") or offer.get("itemCondition") or "").lower()
    is_new = "new" in condition and "used" not in condition
    salvage = taxonomy.looks_salvage(title)

    model = str(node.get("model") or "").strip() or None
    brand = node.get("brand")
    make = (brand.get("name") if isinstance(brand, dict) else brand) or None

    listing = CarListing(
        source=source,
        url=url,
        title=title or url,
        price=price,
        currency=str(offer.get("priceCurrency") or "CAD").upper(),
        year=year,
        make=str(make) if make else None,
        model=model,
        trim=str(node.get("vehicleConfiguration") or "").strip() or None,
        mileage_km=mileage,
        transmission=taxonomy.parse_transmission(str(node.get("vehicleTransmission") or "")),
        fuel=taxonomy.parse_fuel(str(node.get("fuelType") or "")),
        drivetrain=taxonomy.parse_drivetrain(f"{title} {node.get('vehicleConfiguration') or ''}"),
        condition=SALVAGE if salvage else ("new" if is_new else USED),
        channel=RETAIL,
        seller_name=str(seller.get("name") or "").strip() or None,
        seller_type="dealer" if seller.get("@type") == "AutoDealer" else None,
        city=city,
        region=region,
        photos=[i for i in (node.get("image") or []) if isinstance(i, str)][:1],
    )
    listing.distance_km = city_distance(query.place, city, region)
    return listing
