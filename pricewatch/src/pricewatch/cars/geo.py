"""Turning "laval, quebec, canada" into something every source can search.

Each site models geography differently — AutoTrader wants `reg_qc/cit_laval`,
Kijiji wants a numeric location id from its own tree, Copart reports a yard
city. Rather than teach every adapter to parse addresses, a query resolves once
to a `Place` with real coordinates and each adapter maps that to its own scheme.

Coordinates matter beyond tidiness: the sources cannot be trusted to honour a
radius (AutoTrader's Laval search returns Ottawa cars), so the swarm re-filters
on true distance. That only works if we know where the user actually is.
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

PROVINCES = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba",
    "NB": "New Brunswick", "NL": "Newfoundland and Labrador", "NS": "Nova Scotia",
    "NT": "Northwest Territories", "NU": "Nunavut", "ON": "Ontario",
    "PE": "Prince Edward Island", "QC": "Quebec", "SK": "Saskatchewan",
    "YT": "Yukon",
}
_PROVINCE_BY_NAME = {v.lower(): k for k, v in PROVINCES.items()}
_PROVINCE_BY_NAME.update({
    "québec": "QC", "quebec": "QC", "pq": "QC",
    "newfoundland": "NL", "northwest territories": "NT",
    "prince edward island": "PE", "b.c.": "BC", "bc": "BC",
})

# Enough of the country to answer the common case without a network round trip,
# and to keep the offline test suite honest. Anything else geocodes at runtime.
_CITIES: dict[str, tuple[str, float, float]] = {
    "laval": ("QC", 45.6066, -73.7124),
    "montreal": ("QC", 45.5019, -73.5674),
    "montréal": ("QC", 45.5019, -73.5674),
    "longueuil": ("QC", 45.5312, -73.5182),
    "brossard": ("QC", 45.4586, -73.4659),
    "terrebonne": ("QC", 45.7000, -73.6333),
    "gatineau": ("QC", 45.4765, -75.7013),
    "quebec city": ("QC", 46.8139, -71.2080),
    "québec city": ("QC", 46.8139, -71.2080),
    "sherbrooke": ("QC", 45.4042, -71.8929),
    "trois-rivieres": ("QC", 46.3432, -72.5432),
    "toronto": ("ON", 43.6532, -79.3832),
    "mississauga": ("ON", 43.5890, -79.6441),
    "brampton": ("ON", 43.6832, -79.7629),
    "hamilton": ("ON", 43.2557, -79.8711),
    "ottawa": ("ON", 45.4215, -75.6972),
    "london": ("ON", 42.9849, -81.2453),
    "windsor": ("ON", 42.3149, -83.0364),
    "kitchener": ("ON", 43.4516, -80.4925),
    "vancouver": ("BC", 49.2827, -123.1207),
    "surrey": ("BC", 49.1913, -122.8490),
    "burnaby": ("BC", 49.2488, -122.9805),
    "victoria": ("BC", 48.4284, -123.3656),
    "calgary": ("AB", 51.0447, -114.0719),
    "edmonton": ("AB", 53.5461, -113.4938),
    "winnipeg": ("MB", 49.8951, -97.1384),
    "saskatoon": ("SK", 52.1332, -106.6700),
    "regina": ("SK", 50.4452, -104.6189),
    "halifax": ("NS", 44.6488, -63.5752),
    "moncton": ("NB", 46.0878, -64.7782),
    "st. john's": ("NL", 47.5615, -52.7126),
    "charlottetown": ("PE", 46.2382, -63.1311),
}

# Postal FSA (first three characters) is what AutoTrader's proximity search
# takes. Only the ones we ship coordinates for; anything else falls back to a
# city/region path, which those sites also accept.
_FSA = {
    "laval": "H7T", "montreal": "H2X", "montréal": "H2X", "longueuil": "J4K",
    "brossard": "J4W", "terrebonne": "J6W", "gatineau": "J8X",
    "quebec city": "G1R", "sherbrooke": "J1H", "toronto": "M5H",
    "mississauga": "L5B", "ottawa": "K1P", "hamilton": "L8P", "london": "N6A",
    "vancouver": "V6B", "surrey": "V3T", "victoria": "V8W", "calgary": "T2P",
    "edmonton": "T5J", "winnipeg": "R3C", "halifax": "B3J", "saskatoon": "S7K",
    "regina": "S4P", "moncton": "E1C",
}

_POSTAL_RE = re.compile(r"\b([A-Za-z]\d[A-Za-z])\s*\d?[A-Za-z]?\d?\b")


@dataclass
class Place:
    """Where the buyer is."""

    city: str
    region: str = ""                 # province code, e.g. "QC"
    country: str = "CA"
    latitude: float | None = None
    longitude: float | None = None
    postal: str | None = None        # FSA, three characters
    raw: str = ""

    @property
    def slug(self) -> str:
        """URL-safe city, the form AutoTrader and Carpages use in paths."""
        text = self.city.lower()
        for accented, plain in (("é", "e"), ("è", "e"), ("ê", "e"), ("à", "a"),
                                ("â", "a"), ("î", "i"), ("ô", "o"), ("û", "u"),
                                ("ç", "c")):
            text = text.replace(accented, plain)
        text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
        return text

    @property
    def region_name(self) -> str:
        return PROVINCES.get(self.region, "")

    def as_dict(self) -> dict:
        return {"city": self.city, "region": self.region, "country": self.country,
                "latitude": self.latitude, "longitude": self.longitude,
                "postal": self.postal}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance. Good enough to decide "is this car worth driving to"."""
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def _split(text: str) -> tuple[str, str]:
    """"laval, quebec, canada" → ("laval", "QC")."""
    parts = [p.strip() for p in re.split(r"[,/]", text) if p.strip()]
    parts = [p for p in parts if p.lower() not in ("canada", "ca")]
    if not parts:
        return "", ""
    city = parts[0]
    region = ""
    for part in parts[1:]:
        lowered = part.lower()
        if lowered in _PROVINCE_BY_NAME:
            region = _PROVINCE_BY_NAME[lowered]
        elif len(part) == 2 and part.upper() in PROVINCES:
            region = part.upper()
    return city, region


def resolve_offline(text: str) -> Place | None:
    """Resolve from the built-in table only — no network, used by tests."""
    if not text:
        return None
    raw = text.strip()
    postal_match = _POSTAL_RE.search(raw)
    # A postal code sitting inside the text ("Montreal H2X 1Y4, QC") would
    # otherwise become part of the city name and miss the table.
    without_postal = _POSTAL_RE.sub(" ", raw) if postal_match else raw
    city, region = _split(without_postal)
    key = city.lower().strip()
    if key in _CITIES:
        prov, lat, lon = _CITIES[key]
        return Place(city=city.title(), region=region or prov, country="CA",
                     latitude=lat, longitude=lon,
                     postal=(postal_match.group(1).upper() if postal_match
                             else _FSA.get(key)),
                     raw=raw)
    if region:
        return Place(city=city.title() if city else PROVINCES[region],
                     region=region, country="CA",
                     postal=postal_match.group(1).upper() if postal_match else None,
                     raw=raw)
    return None


async def resolve(text: str) -> Place:
    """Resolve a free-text location, geocoding only what the table misses."""
    known = resolve_offline(text)
    if known and known.latitude is not None:
        return known

    geocoded = await _geocode(text)
    if geocoded is not None:
        if known is not None and known.postal and not geocoded.postal:
            geocoded.postal = known.postal
        return geocoded
    return known or Place(city=text.strip(), raw=text)


async def _geocode(text: str) -> Place | None:
    """OpenStreetMap Nominatim: free, keyless, and asks only for a real UA."""
    from ..fetch import fetcher
    from ..settings import settings

    query = text if "canada" in text.lower() else f"{text}, Canada"
    url = ("https://nominatim.openstreetmap.org/search"
           f"?q={_quote(query)}&format=json&limit=1&addressdetails=1")
    try:
        page = await fetcher.get(url, headers={
            "User-Agent": f"hermes-shopping/1.0 ({settings.pw_user_agent[:20]})",
            "Accept": "application/json",
        })
        rows = json.loads(page.text)
    except Exception as exc:                       # noqa: BLE001 — geocoding is optional
        log.debug("geocode failed for %s: %s", text, exc)
        return None
    if not rows:
        return None

    row = rows[0]
    address = row.get("address") or {}
    city = (address.get("city") or address.get("town") or address.get("village")
            or address.get("municipality") or address.get("state_district")
            or row.get("name") or text)
    state = (address.get("state") or "").lower()
    region = _PROVINCE_BY_NAME.get(state, "")
    postal = (address.get("postcode") or "")[:3].upper().strip() or None
    return Place(city=str(city).split("(")[0].strip(), region=region,
                 country=(address.get("country_code") or "ca").upper(),
                 latitude=float(row["lat"]), longitude=float(row["lon"]),
                 postal=postal, raw=text)


def _quote(text: str) -> str:
    from urllib.parse import quote
    return quote(text)


def distance_to(place: Place, lat: float | None, lon: float | None) -> float | None:
    if (place.latitude is None or place.longitude is None
            or lat is None or lon is None):
        return None
    return haversine_km(place.latitude, place.longitude, lat, lon)


def city_distance(place: Place, city: str | None, region: str | None) -> float | None:
    """Distance when a source gives a city name but no coordinates."""
    if not city:
        return None
    entry = _CITIES.get(city.lower().strip())
    if entry is None:
        return None
    _, lat, lon = entry
    if region and entry[0] != region.upper():
        return None
    return distance_to(place, lat, lon)


# ── city → coordinates, for the long tail the table does not carry ──────
# Listings come from wherever the dealers are: Saint-Laurent, Vaudreuil-Dorion,
# Saint-Bruno-de-Montarville. Without coordinates for those, "how far is it"
# is blank for most of the results and the radius filter silently passes
# everything. Open-Meteo's geocoder is keyless, tolerates concurrency, and the
# answers are cached to disk because city coordinates never change.
_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"


class _CityCache:
    """Disk-backed city→(lat, lon) memo. All IO is best effort."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[float, float] | None] = {}
        self._path: str | None = None
        self._loaded = False

    def _resolve_path(self) -> str | None:
        from ..settings import settings
        url = settings.pw_db_url
        if url.startswith("sqlite") and "///" in url:
            import os
            db_path = url.split("///", 1)[-1]
            directory = os.path.dirname(db_path) or "."
            if os.path.isdir(directory):
                return os.path.join(directory, "car_cities.json")
        return None

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._path = self._resolve_path()
        if not self._path:
            return
        try:
            import os
            if os.path.exists(self._path):
                with open(self._path) as handle:
                    raw = json.load(handle)
                for key, value in raw.items():
                    self._data[key] = tuple(value) if value else None
        except Exception as exc:                       # noqa: BLE001
            log.debug("city cache load failed: %s", exc)

    def get(self, key: str):
        self._load()
        return self._data.get(key, "miss")

    def put(self, key: str, value) -> None:
        self._load()
        self._data[key] = value
        if not self._path:
            return
        try:
            import os
            tmp = f"{self._path}.tmp"
            with open(tmp, "w") as handle:
                json.dump({k: list(v) if v else None for k, v in self._data.items()},
                          handle, sort_keys=True)
            os.replace(tmp, self._path)
        except Exception as exc:                       # noqa: BLE001
            log.debug("city cache save failed: %s", exc)


_CITY_CACHE = _CityCache()


def _city_variants(city: str) -> list[str]:
    """Spellings worth trying. Dealers write "St-Bruno", gazetteers say "Saint-Bruno"."""
    base = city.strip()
    out = [base]
    expanded = re.sub(r"\bSt[-\s]", "Saint-", base, flags=re.I)
    expanded = re.sub(r"\bSte[-\s]", "Sainte-", expanded, flags=re.I)
    if expanded != base:
        out.append(expanded)
    # gazetteers hyphenate Quebec saint-names; dealers often do not
    hyphenated = re.sub(r"\b(Saint|Sainte)\s+", r"\1-", expanded, flags=re.I)
    if hyphenated not in out:
        out.append(hyphenated)
    # last resort: the first component, so "Montréal-Est" still lands near Montréal
    head = re.split(r"[-–/]", base)[0].strip()
    if head and head not in out and len(head) > 3:
        out.append(head)
    return out


async def geocode_city(city: str, region: str | None = None,
                       country: str = "CA") -> tuple[float, float] | None:
    """Coordinates for a city name, cached across runs."""
    if not city:
        return None
    key = f"{city.strip().lower()}|{(region or '').lower()}|{country.lower()}"
    cached = _CITY_CACHE.get(key)
    if cached != "miss":
        return cached

    from ..fetch import fetcher
    result: tuple[float, float] | None = None
    for name in _city_variants(city):
        url = (f"{_GEOCODE_URL}?name={_quote(name)}&count=5&language=en&format=json"
               f"&countryCode={country.upper()}")
        try:
            page = await fetcher.get(url, headers={"Accept": "application/json"}, retries=0)
            rows = (json.loads(page.text) or {}).get("results") or []
        except Exception as exc:                       # noqa: BLE001 — distance is a nicety
            log.debug("geocode_city failed for %s: %s", city, exc)
            continue
        if not rows:
            continue
        # Prefer a hit in the right province when the source told us one.
        wanted = PROVINCES.get((region or "").upper(), "").lower()
        best = next((r for r in rows
                     if wanted and str(r.get("admin1", "")).lower() == wanted), rows[0])
        result = (float(best["latitude"]), float(best["longitude"]))
        break

    if result is None:
        result = await _geocode_place_fallback(city, region, country)

    _CITY_CACHE.put(key, result)
    return result


# Rough province centroids. The last rung of the ladder: a listing whose town
# no gazetteer knows still has to land somewhere, because "distance unknown"
# reads as "inside your radius" to the filter and quietly puts Toronto cars in
# a Laval report.
_PROVINCE_CENTROIDS = {
    "AB": (51.0, -114.1), "BC": (49.3, -123.1), "MB": (49.9, -97.1),
    "NB": (46.1, -64.8), "NL": (47.6, -52.7), "NS": (44.6, -63.6),
    "NT": (62.5, -114.4), "NU": (63.7, -68.5), "ON": (43.7, -79.4),
    "PE": (46.2, -63.1), "QC": (45.5, -73.6), "SK": (52.1, -106.7),
    "YT": (60.7, -135.1),
}


async def _geocode_place_fallback(city: str, region: str | None,
                                  country: str) -> tuple[float, float] | None:
    """Nominatim, then the province centroid.

    Nominatim carries neighbourhoods and former municipalities — North York,
    Saint-Hubert — that the lighter gazetteer does not.
    """
    where = ", ".join(filter(None, [city, PROVINCES.get((region or "").upper(), region or ""),
                                    "Canada" if country.upper() == "CA" else country]))
    place = await _geocode(where)
    if place is not None and place.latitude is not None:
        return (place.latitude, place.longitude)
    return _PROVINCE_CENTROIDS.get((region or "").upper())


async def annotate_distances(place: Place, listings) -> None:
    """Fill in `distance_km` for every listing, geocoding unknown cities once each.

    Mutates in place. Sources that already published coordinates keep them; the
    rest are resolved by city name, concurrently, one lookup per distinct city
    rather than one per listing.
    """
    import asyncio

    if place.latitude is None or place.longitude is None:
        return

    pending: dict[tuple[str, str], list] = {}
    for listing in listings:
        if listing.distance_km is not None:
            continue
        if listing.latitude is not None and listing.longitude is not None:
            listing.distance_km = distance_to(place, listing.latitude, listing.longitude)
            continue
        offline = city_distance(place, listing.city, listing.region)
        if offline is not None:
            listing.distance_km = offline
            continue
        if listing.city:
            pending.setdefault((listing.city.strip(), (listing.region or "").strip()),
                               []).append(listing)

    if not pending:
        return

    async def one(key: tuple[str, str]):
        city, region = key
        return key, await geocode_city(city, region, place.country or "CA")

    for key, coords in await asyncio.gather(*(one(k) for k in pending)):
        if coords is None:
            continue
        for listing in pending[key]:
            listing.latitude, listing.longitude = coords
            listing.distance_km = distance_to(place, *coords)
