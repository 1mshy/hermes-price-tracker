"""Reading vehicle facts out of the words a listing happens to use.

Two problems this solves. First, sources publish the same fact in different
shapes — "61,351 km", "61 351 KM", "38,000 miles" are one odometer reading.
Second, this is Quebec: roughly half the listings on Kijiji and the dealer
sites are in French, and a report that treats "Automatique / traction
intégrale / cuir" as unparseable throws away the local half of the market.
"""
from __future__ import annotations

import re

MILES_TO_KM = 1.60934

# ── odometer ────────────────────────────────────────────────────────────
# Thin spaces and non-breaking spaces are the French thousands separator and
# show up constantly on Quebec listings.
_KM_RE = re.compile(
    r"(\d[\d\s,.  ']*)\s*(km\b|kilom[eè]tres?\b|kms\b)", re.I)
_MI_RE = re.compile(
    r"(\d[\d\s,.  ']*)\s*(mi\b|miles?\b|milles?\b)", re.I)


def _digits(raw: str) -> int | None:
    cleaned = re.sub(r"[\s,.  ']", "", raw)
    if not cleaned.isdigit():
        return None
    value = int(cleaned)
    return value if 0 < value < 2_000_000 else None


def parse_mileage_km(text: str | None) -> int | None:
    """Odometer in kilometres, converting miles where the listing used them."""
    if not text:
        return None
    match = _KM_RE.search(text)
    if match:
        return _digits(match.group(1))
    match = _MI_RE.search(text)
    if match:
        miles = _digits(match.group(1))
        return int(round(miles * MILES_TO_KM)) if miles is not None else None
    return None


# ── model year ──────────────────────────────────────────────────────────
_YEAR_RE = re.compile(r"\b(19[7-9]\d|20[0-4]\d)\b")


def parse_year(text: str | None) -> int | None:
    """First plausible model year in a title. Titles lead with it by convention."""
    if not text:
        return None
    match = _YEAR_RE.search(text)
    return int(match.group(1)) if match else None


# ── transmission / drivetrain / fuel, in both official languages ────────
_TRANSMISSION = (
    (r"\b(manual|manuelle?|standard|\d\s*-?\s*speed manual|bo[îi]te manuelle)\b", "manual"),
    (r"\b(automat\w*|auto|cvt|tiptronic|s[- ]?tronic|dsg|pdk|dct)\b", "automatic"),
)
_DRIVETRAIN = (
    (r"\b(awd|all[- ]wheel|quattro|4matic|xdrive|4motion|traction int[ée]grale|"
     r"int[ée]grale|sh[- ]awd|4x4|4wd|two[- ]?motor)\b", "awd"),
    (r"\b(rwd|rear[- ]wheel|propulsion)\b", "rwd"),
    (r"\b(fwd|front[- ]wheel|traction avant)\b", "fwd"),
)
_FUEL = (
    (r"\b(plug[- ]?in|phev|hybride rechargeable)\b", "plug-in hybrid"),
    (r"\b(hybrid\w*|hybride)\b", "hybrid"),
    (r"\b(electric|[ée]lectrique|ev\b|bev)\b", "electric"),
    (r"\b(diesel|tdi)\b", "diesel"),
    (r"\b(gas\w*|essence|petrol|tfsi|tsi)\b", "gas"),
)
# Title brands. A rebuilt car looks like a bargain until you learn why.
_SALVAGE = (
    r"\b(salvage|rebuilt|reconstruit\w*|irr[ée]parable|branded|"
    r"flood|write[- ]?off|vga|s[ée]v[èe]rement accident[ée]|non[- ]r[ée]parable)\b"
)


def _first(patterns, text: str) -> str | None:
    for pattern, value in patterns:
        if re.search(pattern, text, re.I):
            return value
    return None


def parse_transmission(text: str | None) -> str | None:
    return _first(_TRANSMISSION, text or "")


def parse_drivetrain(text: str | None) -> str | None:
    return _first(_DRIVETRAIN, text or "")


def parse_fuel(text: str | None) -> str | None:
    return _first(_FUEL, text or "")


def looks_salvage(text: str | None) -> bool:
    return bool(re.search(_SALVAGE, text or "", re.I))


# ── make / model normalisation ──────────────────────────────────────────
# Sites slug names differently and shoppers type them a third way. Normalising
# to one spelling is what lets "Mercedes A250", "mercedes-benz a 250" and
# "Mercedes Benz" all reach the same search.
_MAKE_ALIASES = {
    "vw": "volkswagen", "chevy": "chevrolet", "mercedes": "mercedes-benz",
    "mercedes benz": "mercedes-benz", "merc": "mercedes-benz",
    "landrover": "land-rover", "land rover": "land-rover",
    "alfa": "alfa-romeo", "alfa romeo": "alfa-romeo",
    "rangerover": "land-rover", "range rover": "land-rover",
    "gmc truck": "gmc", "mini cooper": "mini", "vauxhall": "opel",
}

_STOP_TOKENS = {
    "for", "sale", "used", "new", "certified", "cpo", "pre-owned", "preowned",
    "occasion", "usage", "usagé", "neuf", "à", "vendre", "vendre", "de",
    "the", "a", "with", "avec", "and", "et", "km", "kms",
}


def normalise_make(make: str | None) -> str:
    if not make:
        return ""
    text = re.sub(r"[^a-z0-9\- ]", " ", make.lower()).strip()
    text = re.sub(r"\s+", " ", text)
    return _MAKE_ALIASES.get(text, text).replace(" ", "-")


def normalise_model(model: str | None) -> str:
    """Model as a comparison key: lowercase, punctuation-free, spaces closed up.

    "A3 Sportback", "a3-sportback" and "A 3 Sportback" are one model; keeping
    them apart would split a search's results into three piles that never
    dedupe against each other.
    """
    if not model:
        return ""
    text = re.sub(r"[^a-z0-9]+", "", model.lower())
    return text


def model_slug(model: str | None) -> str:
    """Model as sites spell it in a URL path: lowercase, hyphenated."""
    if not model:
        return ""
    text = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
    return text


def model_matches(wanted: str | None, candidate: str | None) -> bool:
    """Is `candidate` the model the shopper asked for?

    Deliberately prefix-based rather than equality: a search for "A3" should
    keep "A3 Sportback" and "A3 quattro", because those are trims of the car
    they asked about. It must still reject "A4" and "Q3", which a plain
    substring test would not (both contain neither, but "A3" is a substring of
    "A3" *and* of nothing else here — the risk is the reverse direction, where
    candidate "A" would match wanted "A3").
    """
    want, got = normalise_model(wanted), normalise_model(candidate)
    if not want or not got:
        return False
    if want == got:
        return True
    # "a3" vs "a3sportback" — the candidate is a longer, more specific name.
    if got.startswith(want) and not got[len(want):len(want) + 1].isdigit():
        return True
    if want.startswith(got) and not want[len(got):len(got) + 1].isdigit():
        return True
    return False


def extract_trim(title: str | None, year: int | None,
                 make: str | None, model: str | None) -> str | None:
    """Whatever is left of a title once the year, make and model are removed.

    Crude on purpose — trim naming has no standard, and a wrong guess here only
    affects a label, never a price or a match.
    """
    if not title:
        return None
    text = title
    if year:
        text = re.sub(rf"\b{year}\b", " ", text)
    for token in (make or "", model or ""):
        if token:
            text = re.sub(rf"\b{re.escape(token)}\b", " ", text, flags=re.I)
    text = re.sub(r"[|/,]+", " ", text)
    text = _KM_RE.sub(" ", text)
    words = [w for w in re.split(r"\s+", text) if w and w.lower() not in _STOP_TOKENS]
    trim = " ".join(words[:5]).strip(" -–—!.")
    return trim or None
