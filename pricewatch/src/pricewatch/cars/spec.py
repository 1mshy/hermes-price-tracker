"""What the shopper asked for, and whether a given car actually answers it.

The sources cannot be trusted to filter. AutoTrader returns Winnipeg cars for a
Laval search, Kijiji's keyword search happily hands back a Q3 when you asked
for an A3, and Copart's free-text search matches on the make alone. So the
query keeps its own predicate and every listing is re-checked locally — the
filtering the sites claim to do is treated as a hint, not a contract.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from . import taxonomy
from .geo import Place, resolve, resolve_offline
from .listing import AUCTION, CarListing, SALVAGE

# Enough of the marques sold in Canada to pull a make out of free text. Order
# matters: multi-word names must be tried before their first word, or "land
# rover discovery" resolves to make "land", model "rover discovery".
MAKES = (
    "alfa romeo", "aston martin", "land rover", "range rover", "mercedes-benz",
    "mercedes benz", "rolls royce", "great wall",
    "acura", "audi", "bentley", "bmw", "buick", "cadillac", "chevrolet", "chevy",
    "chrysler", "dodge", "ferrari", "fiat", "ford", "genesis", "gmc", "honda",
    "hummer", "hyundai", "infiniti", "jaguar", "jeep", "kia", "lamborghini",
    "lexus", "lincoln", "lotus", "maserati", "mazda", "mclaren", "mercedes",
    "mini", "mitsubishi", "nissan", "polestar", "pontiac", "porsche", "ram",
    "rivian", "saab", "saturn", "scion", "smart", "subaru", "suzuki", "tesla",
    "toyota", "volkswagen", "volvo", "vw", "lucid",
)

# No bare "a": in "looking for a cheap Golf GTI around Montreal" it matches the
# English article and swallows the whole sentence as a place name.
_LOCATION_RE = re.compile(
    r"\b(?:in|near|around|close to|within|à|proche de|autour de|près de)\s+(.+)$", re.I)
_YEAR_RANGE_RE = re.compile(r"\b(19[7-9]\d|20[0-4]\d)\s*(?:-|–|to|through|a|à)\s*(19[7-9]\d|20[0-4]\d)\b")
_YEAR_RE = re.compile(r"\b(19[7-9]\d|20[0-4]\d)\b")
_RADIUS_RE = re.compile(r"\bwithin\s+(\d{1,4})\s*(km|kilometres?|kilometers?|mi|miles?)\b", re.I)
_MAX_PRICE_RE = re.compile(r"\bunder\s*\$?\s*([\d,\s]{3,10})\b", re.I)
_MAX_KM_RE = re.compile(r"\b(?:under|less than|below|max)\s*([\d,\s]{3,10})\s*(?:km|kms)\b", re.I)
# Where a location phrase stops and the next filter starts.
_CLAUSE_END_RE = re.compile(
    r"\s+(?:under|below|less than|within|with|over|above|max|min|for|that|"
    r"nothing|budget|but|and|cheaper than|no more than|sous|moins de)\b", re.I)


@dataclass
class CarQuery:
    """A vehicle search, resolved and self-filtering."""

    make: str
    model: str
    place: Place
    year_min: int | None = None
    year_max: int | None = None
    radius_km: float = 150.0
    price_max: Decimal | None = None
    price_min: Decimal | None = None
    max_mileage_km: int | None = None
    trim: str | None = None
    include_auctions: bool = True
    include_salvage: bool = False
    limit_per_source: int = 60
    raw: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def years(self) -> list[int]:
        if self.year_min is None and self.year_max is None:
            return []
        low = self.year_min or self.year_max
        high = self.year_max or self.year_min
        return list(range(low, high + 1))

    @property
    def label(self) -> str:
        years = ""
        if self.year_min and self.year_max and self.year_min != self.year_max:
            years = f"{self.year_min}–{self.year_max} "
        elif self.year_min or self.year_max:
            years = f"{self.year_min or self.year_max} "
        where = self.place.city or self.place.region_name
        return f"{years}{self.make.title()} {self.model.upper()}".strip() + (
            f" near {where}" if where else "")

    def year_ok(self, year: int | None) -> bool:
        if self.year_min is None and self.year_max is None:
            return True
        if year is None:
            return False            # a year filter with no year is a miss, not a pass
        if self.year_min is not None and year < self.year_min:
            return False
        if self.year_max is not None and year > self.year_max:
            return False
        return True

    def matches(self, listing: CarListing) -> tuple[bool, str | None]:
        """Does this listing answer the question? Returns (keep, why-not)."""
        if not self.year_ok(listing.year):
            return False, f"year {listing.year}"
        if listing.make and taxonomy.normalise_make(listing.make) != taxonomy.normalise_make(self.make):
            return False, f"make {listing.make}"
        if listing.model and not taxonomy.model_matches(self.model, listing.model):
            return False, f"model {listing.model}"
        if not listing.model and listing.title:
            # Sources that publish no structured model (Carpages, Copart) still
            # put it in the title; require it there rather than trusting the
            # site's own search to have filtered.
            if not _title_mentions(listing.title, self.model):
                return False, "model not in title"
        if listing.condition == SALVAGE and not self.include_salvage:
            return False, "salvage"
        if listing.channel == AUCTION and not self.include_auctions:
            return False, "auction"
        price = listing.effective_price
        if self.price_max is not None and price is not None and price > self.price_max:
            return False, f"over {self.price_max}"
        if self.price_min is not None and price is not None and price < self.price_min:
            return False, f"under {self.price_min}"
        if self.max_mileage_km is not None and listing.mileage_km is not None \
                and listing.mileage_km > self.max_mileage_km:
            return False, f"{listing.mileage_km} km"
        if (self.radius_km and listing.distance_km is not None
                and listing.distance_km > self.radius_km):
            return False, f"{listing.distance_km:.0f} km away"
        return True, None

    def as_dict(self) -> dict:
        return {
            "make": self.make, "model": self.model,
            "year_min": self.year_min, "year_max": self.year_max,
            "location": self.place.as_dict(), "radius_km": self.radius_km,
            "price_max": float(self.price_max) if self.price_max else None,
            "max_mileage_km": self.max_mileage_km,
            "include_auctions": self.include_auctions,
            "include_salvage": self.include_salvage,
            "label": self.label,
        }


def _title_mentions(title: str, model: str) -> bool:
    normalised = taxonomy.normalise_model(title)
    wanted = taxonomy.normalise_model(model)
    if not wanted:
        return True
    if wanted not in normalised:
        return False
    # "a3" must not match inside "a35" or "ca3" — require a non-alphanumeric or
    # string boundary on each side of the match in the original text.
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(model.lower())}(?![a-z0-9])",
                          title.lower()))


def parse_free_text(text: str) -> dict:
    """Best-effort structure from a sentence, with no LLM in the loop.

    This is the fallback path and the test surface: the agent normally passes
    make/model/year explicitly, and `analyst.parse_query` can use the model for
    anything shaped oddly, but neither should be required to search.
    """
    lowered = " " + text.lower().strip() + " "
    out: dict = {"raw": text}

    range_match = _YEAR_RANGE_RE.search(lowered)
    if range_match:
        out["year_min"], out["year_max"] = int(range_match.group(1)), int(range_match.group(2))
    else:
        years = _YEAR_RE.findall(lowered)
        if years:
            out["year_min"] = out["year_max"] = int(years[0])

    make = next((m for m in MAKES if re.search(rf"(?<![a-z]){re.escape(m)}(?![a-z])", lowered)), None)
    if make:
        out["make"] = taxonomy.normalise_make(make)
        tail = lowered.split(make, 1)[1]
        # model is the token right after the make, minus a possessive plural
        # ("a3's") and anything that starts the location clause
        tail = _LOCATION_RE.sub(" ", tail)
        tokens = [t for t in re.split(r"[\s,]+", tail) if t]
        if tokens:
            model = tokens[0].strip("'’s.")
            model = re.sub(r"['’]s$", "", model)
            if model and not _YEAR_RE.fullmatch(model):
                out["model"] = model

    location = _LOCATION_RE.search(text.strip())
    if location:
        # "near Toronto under $25,000 within 50 km" — the place ends where the
        # next constraint clause begins, or the city swallows the whole tail
        # and geocodes to nothing.
        where = _CLAUSE_END_RE.split(location.group(1))[0]
        out["location"] = where.strip(" .?!,")

    radius = _RADIUS_RE.search(lowered)
    if radius:
        value = float(radius.group(1))
        out["radius_km"] = value * taxonomy.MILES_TO_KM if radius.group(2).startswith("mi") else value

    price = _MAX_PRICE_RE.search(lowered)
    if price:
        digits = re.sub(r"[,\s]", "", price.group(1))
        if digits.isdigit():
            out["price_max"] = float(digits)

    max_km = _MAX_KM_RE.search(lowered)
    if max_km:
        digits = re.sub(r"[,\s]", "", max_km.group(1))
        if digits.isdigit():
            out["max_mileage_km"] = int(digits)

    if re.search(r"\b(salvage|rebuilt|reconstruit|write[- ]?off)\b", lowered):
        out["include_salvage"] = True
    return out


async def build(make: str, model: str, location: str, *, year: int | None = None,
                year_min: int | None = None, year_max: int | None = None,
                radius_km: float = 150.0, price_max: float | None = None,
                price_min: float | None = None, max_mileage_km: int | None = None,
                trim: str | None = None, include_auctions: bool = True,
                include_salvage: bool = False, limit_per_source: int = 60,
                raw: str = "") -> CarQuery:
    """Resolve a query, geocoding the location."""
    place = await resolve(location) if location else Place(city="")
    if year is not None:
        year_min = year_max = year
    return CarQuery(
        make=taxonomy.normalise_make(make), model=model.strip(), place=place,
        year_min=year_min, year_max=year_max, radius_km=radius_km,
        price_max=Decimal(str(price_max)) if price_max else None,
        price_min=Decimal(str(price_min)) if price_min else None,
        max_mileage_km=max_mileage_km, trim=trim,
        include_auctions=include_auctions, include_salvage=include_salvage,
        limit_per_source=limit_per_source, raw=raw or f"{make} {model} {location}")


def build_offline(make: str, model: str, location: str, **kwargs) -> CarQuery:
    """Same as `build` without the geocoder — used by tests."""
    place = resolve_offline(location) or Place(city=location)
    year = kwargs.pop("year", None)
    if year is not None:
        kwargs["year_min"] = kwargs["year_max"] = year
    price_max = kwargs.pop("price_max", None)
    price_min = kwargs.pop("price_min", None)
    return CarQuery(make=taxonomy.normalise_make(make), model=model.strip(), place=place,
                    price_max=Decimal(str(price_max)) if price_max else None,
                    price_min=Decimal(str(price_min)) if price_min else None,
                    **kwargs)
