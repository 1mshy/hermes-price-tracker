"""Copart.ca — the salvage auction floor.

Included because it answers a question the retail sites cannot: what is this
car worth when it is broken? A 2022 A3 with front-end damage going for $6,000
is the floor under every asking price in the report, and for a buyer willing to
do the work it is a real option.

It is also the source most likely to mislead. These cars are damaged, the bid
shown is not the price you pay (buyer fees and taxes land on top), and the VIN
is masked for logged-out visitors. So every lot is marked `salvage`/`auction`,
which keeps it out of the retail ranking unless the shopper asked for it.

The public search API is a plain JSON POST with two-letter field names —
`lcy` year, `mkn` make, `lm` model, `hb` high bid, `orr` odometer, `dd` damage.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from .. import taxonomy
from ..geo import distance_to
from ..listing import AUCTION, SALVAGE, CarListing
from ..spec import CarQuery
from .base import CarSource

BASE = "https://www.copart.ca"
SEARCH_URL = f"{BASE}/public/lots/search-results"
PAGE_SIZE = 100


class CopartSource(CarSource):
    key = "copart"
    label = "Copart Canada (salvage auction)"
    channel = AUCTION

    def supports(self, query: CarQuery) -> bool:
        # Pointless work when the shopper does not want damaged cars.
        return super().supports(query) and query.include_auctions

    def build_body(self, query: CarQuery) -> dict:
        years = query.years
        terms = " ".join(filter(None, [
            str(years[0]) if len(years) == 1 else "",
            query.make, query.model,
        ])).strip()
        return {
            "query": [terms or f"{query.make} {query.model}"],
            "filter": {},
            "sort": ["auction_date_type desc"],
            "page": 0,
            "size": PAGE_SIZE,
        }

    async def search(self, query: CarQuery) -> list[CarListing]:
        from ...fetch import fetcher
        session = fetcher._cffi_session()          # noqa: SLF001 — POST is not on the shared surface
        if session is None:
            raise RuntimeError("curl_cffi unavailable; Copart needs a POST with a browser fingerprint")
        response = await session.post(
            SEARCH_URL, json=self.build_body(query),
            headers={"Accept": "application/json", "Content-Type": "application/json"})
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code} from copart search")
        payload = response.json()
        return parse_results(payload, query, source=self.key)


def parse_results(payload: dict, query: CarQuery, *, source: str = "copart") -> list[CarListing]:
    content = (((payload or {}).get("data") or {}).get("results") or {}).get("content") or []
    out: list[CarListing] = []
    for lot in content:
        listing = _listing_from_lot(lot, query, source)
        if listing is not None:
            out.append(listing)
    return out


def _listing_from_lot(lot: dict, query: CarQuery, source: str) -> CarListing | None:
    if not isinstance(lot, dict):
        return None
    lot_number = lot.get("lotNumberStr") or lot.get("ln")
    if not lot_number:
        return None
    slug = lot.get("ldu") or ""
    url = f"{BASE}/lot/{lot_number}" + (f"/{slug}" if slug else "")

    year = lot.get("lcy") if isinstance(lot.get("lcy"), int) else taxonomy.parse_year(str(lot.get("ld") or ""))
    bid = lot.get("hb")
    current_bid = Decimal(str(bid)) if isinstance(bid, (int, float)) and bid > 0 else None

    odometer = lot.get("orr")
    mileage = int(odometer) if isinstance(odometer, (int, float)) and odometer > 0 else None
    # Copart records US lots in miles; Canadian yards in kilometres.
    if mileage and str(lot.get("locCountry") or "").upper() in ("USA", "US"):
        mileage = int(round(mileage * taxonomy.MILES_TO_KM))

    auction_at = None
    stamp = lot.get("ad")
    if isinstance(stamp, (int, float)) and stamp > 0:
        try:
            auction_at = dt.datetime.fromtimestamp(stamp / 1000, tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            auction_at = None

    latitude, longitude = lot.get("lat"), lot.get("long")
    vin = str(lot.get("fv") or "")
    if "*" in vin:                                  # masked for logged-out visitors
        vin = ""

    listing = CarListing(
        source=source,
        url=url,
        title=str(lot.get("ld") or f"{year} {lot.get('mkn')} {lot.get('lm')}").strip(),
        # An auction has no asking price. Leaving `price` unset is what keeps
        # these lots out of the retail statistics rather than dragging the
        # market average down to the value of a wreck.
        price=None,
        current_bid=current_bid,
        currency=str(lot.get("cuc") or "CAD").upper(),
        year=year,
        make=str(lot.get("mkn") or lot.get("lmc") or "") or None,
        model=str(lot.get("lm") or lot.get("lmg") or "") or None,
        trim=str(lot.get("ltd") or "") or None,
        mileage_km=mileage,
        vin=vin or None,
        drivetrain=taxonomy.parse_drivetrain(str(lot.get("drv") or "")),
        fuel=taxonomy.parse_fuel(str(lot.get("ft") or "")),
        body=str(lot.get("bstl") or "") or None,
        exterior_color=str(lot.get("clr") or "") or None,
        condition=SALVAGE,
        channel=AUCTION,
        seller_name="Copart",
        seller_type="auction",
        city=str(lot.get("locCity") or "").title() or None,
        region=str(lot.get("locState") or "") or None,
        latitude=latitude if isinstance(latitude, (int, float)) else None,
        longitude=longitude if isinstance(longitude, (int, float)) else None,
        auction_ends_at=auction_at,
        extra={k: v for k, v in {
            "damage": lot.get("dd"),
            "secondary_damage": lot.get("dtc"),
            "title_code": lot.get("lcc"),
            "sale_type": lot.get("ess"),
            "estimated_retail_value": lot.get("la"),
            "engine": lot.get("egn"),
            "runs_and_drives": lot.get("hk"),
        }.items() if v not in (None, "", 0.0)},
    )
    listing.distance_km = distance_to(query.place, listing.latitude, listing.longitude)
    return listing
