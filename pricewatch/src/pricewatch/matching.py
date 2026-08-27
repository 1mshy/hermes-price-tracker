"""Deciding whether two store listings are the same product.

Retailers name the same item wildly differently ("Bambu Lab X1-Carbon Combo"
vs "BambuLab X1C 3D Printer w/ AMS"), so exact title equality is useless. We
normalise, weight distinctive model tokens, and require a similarity floor.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

# Words that carry no signal for identity but inflate similarity scores.
_STOPWORDS = {
    "the", "a", "an", "for", "with", "and", "or", "of", "in", "to", "new", "genuine",
    "official", "original", "authentic", "brand", "free", "shipping", "sale", "deal",
    "pack", "set", "kit", "bundle", "combo", "version", "edition", "series", "model",
    "printer", "3d", "filament", "spool", "roll",
}

_UNIT_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*(kg|g|mm|cm|m|w|v|a|ml|l|in|inch|tb|gb|hz)\b")
# Model designators: MK4S, X1C, A1-mini, 9800X3D, RTX4090, SV06, P1S…
_MODEL_RE = re.compile(r"\b([a-z]{1,4}[-]?\d{1,5}[a-z0-9-]{0,6}|\d{3,5}[a-z]{1,4}\d?)\b")

# Names the community and retailers use interchangeably. Without these,
# "Bambu Lab X1-Carbon" and "BambuLab X1C" clash on model tokens and get
# treated as different products. Applied in order, on the lowered raw text.
_ALIASES: tuple[tuple[str, str], ...] = (
    ("bambulab", "bambu lab"),
    ("bambu labs", "bambu lab"),
    ("x1 carbon", "x1c"),
    ("x1-carbon", "x1c"),
    ("x1carbon", "x1c"),
)


def normalise(text: str | None) -> str:
    if not text:
        return ""
    lowered = text.lower()
    lowered = lowered.replace("®", " ").replace("™", " ").replace("&", " and ")
    for alias, canonical in _ALIASES:
        lowered = lowered.replace(alias, canonical)
    lowered = _UNIT_RE.sub(r"\1\2", lowered)          # "1.75 mm" → "1.75mm"
    lowered = re.sub(r"[^a-z0-9.\-\s]", " ", lowered)
    tokens = [t for t in lowered.split() if t and t not in _STOPWORDS]
    return " ".join(tokens)


def model_tokens(text: str | None) -> set[str]:
    """Distinctive alphanumeric designators — the strongest identity signal."""
    return {m.group(1).replace("-", "") for m in _MODEL_RE.finditer(normalise(text))}


def match_key(title: str | None, brand: str | None = None, model_number: str | None = None) -> str:
    parts = [normalise(brand), normalise(title)]
    if model_number:
        parts.append(normalise(model_number))
    return " ".join(p for p in parts if p)[:500]


def score(query: str, candidate: str) -> float:
    """0–100 similarity, boosted when model designators agree, penalised when they clash."""
    left, right = normalise(query), normalise(candidate)
    if not left or not right:
        return 0.0

    base = max(fuzz.token_set_ratio(left, right), fuzz.partial_token_sort_ratio(left, right))

    # token_set_ratio scores a subset title ("PLA") ~100 against a specific query
    # ("PolyTerra PLA 1kg"). Weight by how much of the query the candidate covers
    # so vague listings cannot outrank the product actually asked for.
    query_tokens = set(left.split())
    candidate_tokens = set(right.split())
    if query_tokens:
        coverage = len(query_tokens & candidate_tokens) / len(query_tokens)
        base *= 0.6 + 0.4 * coverage

    q_models, c_models = model_tokens(query), model_tokens(candidate)
    if q_models and c_models:
        if q_models & c_models:
            base = min(100.0, base + 12)
        elif base < 95:
            # Same family, different model (MK4S vs MK3S) — that is a different product.
            base -= 25
    return max(0.0, min(100.0, base))


def search_terms(description: str, max_tokens: int = 4) -> str:
    """Compact search core of a product description.

    Store search endpoints (Shopify suggest, Woo Store API, retail APIs) are
    AND-ish: every extra token narrows the result set, so a fully-specified
    description ("SUNLU AMS Heater compatible with Bambu Lab AMS Gen 1
    filament dryer 4-spool 70C smart humidity control") returns *nothing* even
    when the product is listed. Search with the distinctive core — leading
    brand/product tokens plus any model designators — and rank the results
    against the full description afterwards.
    """
    norm = normalise(description)
    tokens = norm.split()
    if len(tokens) <= max_tokens:
        return norm

    models = model_tokens(description)
    picked: list[str] = []
    for index, token in enumerate(tokens):
        if token in picked:
            continue
        # The first few tokens are almost always brand + product name; model
        # designators are kept wherever they appear ("… MK4S kit").
        if index < max_tokens - 1 or token.replace("-", "") in models:
            picked.append(token)
    return " ".join(picked[:max_tokens + 1])


def is_match(query: str, candidate: str, threshold: float = 72.0) -> bool:
    return score(query, candidate) >= threshold


def rank(query: str, candidates: list, key=lambda c: c.title, threshold: float = 60.0) -> list:
    """Return candidates sorted best-first, dropping anything under the floor."""
    scored = [(score(query, key(c) or ""), c) for c in candidates]
    kept = [(s, c) for s, c in scored if s >= threshold]
    kept.sort(key=lambda pair: pair[0], reverse=True)
    return [c for _, c in kept]
