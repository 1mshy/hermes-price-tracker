"""Turning a pile of listings into an answer about price and tradeoffs.

Deliberately arithmetic rather than judgement. The model writes the prose, but
every number it is given comes from here, so the same query twice produces the
same figures and a wrong claim can be traced to a formula instead of a mood.

Three ideas do most of the work:

* **A local price curve.** Asking price for one model-year is mostly a function
  of odometer. Fitting price against mileage across the cars actually for sale
  near this buyer gives an expected price for any given mileage, and a listing's
  distance from that line is a far better "is this a deal" signal than its
  distance from the average — the cheapest car in the list is usually just the
  one with the most kilometres on it.
* **The Pareto frontier.** "Best price" alone is the wrong question when
  mileage varies. The cars worth considering are the ones nothing else beats on
  both price and odometer at once; everything else is dominated and can be
  dropped from the shortlist without argument.
* **Dealer posture.** Grouping by seller says which lots near you price above
  or below the local curve, which is what decides where to walk in.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from decimal import Decimal

from .listing import AUCTION, CarListing

#: Below this many priced cars, a fitted curve is noise; fall back to the median.
MIN_FOR_CURVE = 5


def _prices(listings: list[CarListing]) -> list[float]:
    return [float(x.price) for x in listings if x.price is not None and x.price > 0]


@dataclass
class MarketStats:
    count: int = 0
    low: float | None = None
    high: float | None = None
    median: float | None = None
    mean: float | None = None
    p25: float | None = None
    p75: float | None = None
    median_mileage: int | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def market_stats(listings: list[CarListing]) -> MarketStats:
    values = sorted(_prices(listings))
    if not values:
        return MarketStats()
    mileages = sorted(x.mileage_km for x in listings if x.mileage_km)
    quantiles = statistics.quantiles(values, n=4) if len(values) >= 4 else None
    return MarketStats(
        count=len(values),
        low=values[0],
        high=values[-1],
        median=statistics.median(values),
        mean=round(statistics.fmean(values), 2),
        p25=round(quantiles[0], 2) if quantiles else None,
        p75=round(quantiles[2], 2) if quantiles else None,
        median_mileage=int(statistics.median(mileages)) if mileages else None,
    )


@dataclass
class PriceCurve:
    """price ≈ intercept + slope × mileage_km, fitted on the local market."""

    intercept: float
    slope: float
    r_squared: float
    points: int

    def expected(self, mileage_km: int | None) -> float | None:
        if mileage_km is None:
            return None
        return self.intercept + self.slope * mileage_km

    def as_dict(self) -> dict:
        return {"intercept": round(self.intercept, 2),
                "dollars_per_1000km": round(self.slope * 1000, 2),
                "r_squared": round(self.r_squared, 3),
                "points": self.points}


def fit_price_curve(listings: list[CarListing]) -> PriceCurve | None:
    """Least squares of price on odometer, rejected when it is not believable.

    A positive slope means the data says more kilometres cost more money. That
    is a sampling artefact, not a market, and quoting an "expected price" from
    it would be worse than saying nothing — so it is thrown away.
    """
    points = [(float(x.mileage_km), float(x.price)) for x in listings
              if x.mileage_km and x.price is not None and x.price > 0]
    if len(points) < MIN_FOR_CURVE:
        return None

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    variance = sum((x - mean_x) ** 2 for x in xs)
    if variance <= 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / variance
    intercept = mean_y - slope * mean_x
    if slope >= 0:
        return None

    total = sum((y - mean_y) ** 2 for y in ys)
    residual = sum((y - (intercept + slope * x)) ** 2 for x, y in points)
    r_squared = 1 - residual / total if total > 0 else 0.0
    return PriceCurve(intercept=intercept, slope=slope,
                      r_squared=max(0.0, r_squared), points=len(points))


def score_listings(listings: list[CarListing], curve: PriceCurve | None,
                   stats: MarketStats) -> None:
    """Attach `expected_price` and `deal_score` (percent under expected) in place."""
    for item in listings:
        if item.price is None or item.channel == AUCTION:
            continue
        expected = curve.expected(item.mileage_km) if curve else None
        basis = "mileage-adjusted"
        if expected is None:
            # No odometer, or no usable curve: fall back to the median asking
            # price. Weaker, and recorded as such — a listing with no stated
            # odometer scored against the median looked like the best deal in
            # the market purely because nobody said how far it had been driven.
            expected = stats.median
            basis = "median-only"
        if not expected or expected <= 0:
            continue
        item.expected_price = Decimal(str(round(expected, 2)))
        item.deal_score = (expected - float(item.price)) / expected * 100
        item.extra = dict(item.extra)
        item.extra["score_basis"] = basis


def pareto_frontier(listings: list[CarListing]) -> list[CarListing]:
    """Cars that nothing else beats on price *and* odometer simultaneously.

    This is the honest shortlist: every car not on it is strictly worse than
    something else on both axes, so no preference between money and kilometres
    could pick it.
    """
    usable = [x for x in listings
              if x.price is not None and x.mileage_km and x.channel != AUCTION]
    frontier: list[CarListing] = []
    for candidate in usable:
        dominated = any(
            other is not candidate
            and float(other.price) <= float(candidate.price)
            and other.mileage_km <= candidate.mileage_km
            and (float(other.price) < float(candidate.price)
                 or other.mileage_km < candidate.mileage_km)
            for other in usable
        )
        if not dominated:
            frontier.append(candidate)
    frontier.sort(key=lambda x: float(x.price))
    return frontier


@dataclass
class DealerPosture:
    name: str
    city: str | None
    units: int
    cheapest: float
    median_deal_score: float | None
    distance_km: float | None
    urls: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"dealer": self.name, "city": self.city, "units": self.units,
                "cheapest": self.cheapest,
                "median_deal_score": (round(self.median_deal_score, 1)
                                      if self.median_deal_score is not None else None),
                "distance_km": round(self.distance_km, 1) if self.distance_km is not None else None,
                "listings": self.urls[:4]}


def dealer_postures(listings: list[CarListing]) -> list[DealerPosture]:
    """Which sellers near this buyer hold the car, and how they price it.

    This is the "local dealerships" view. It is built from the aggregator
    records rather than by crawling dealer websites, because the dealers
    syndicate their inventory precisely so it can be found — and every figure
    here therefore has a live listing URL behind it.
    """
    groups: dict[str, list[CarListing]] = {}
    for item in listings:
        if item.channel == AUCTION or item.price is None:
            continue
        name = (item.seller_name or "").strip()
        if not name or item.seller_type == "private":
            continue
        groups.setdefault(name, []).append(item)

    out: list[DealerPosture] = []
    for name, group in groups.items():
        scores = [x.deal_score for x in group if x.deal_score is not None]
        distances = [x.distance_km for x in group if x.distance_km is not None]
        out.append(DealerPosture(
            name=name,
            city=next((x.city for x in group if x.city), None),
            units=len(group),
            cheapest=min(float(x.price) for x in group),
            median_deal_score=statistics.median(scores) if scores else None,
            distance_km=min(distances) if distances else None,
            urls=[x.url for x in group],
        ))
    out.sort(key=lambda d: (-d.units, d.cheapest))
    return out


@dataclass
class Analysis:
    stats: MarketStats
    curve: PriceCurve | None
    frontier: list[CarListing]
    dealers: list[DealerPosture]
    best_value: CarListing | None = None
    cheapest: CarListing | None = None
    lowest_mileage: CarListing | None = None
    closest: CarListing | None = None
    private_stats: MarketStats | None = None
    dealer_stats: MarketStats | None = None
    auction_floor: float | None = None

    def as_dict(self) -> dict:
        def ref(item: CarListing | None) -> dict | None:
            return item.as_dict() if item is not None else None
        return {
            "market": self.stats.as_dict(),
            "price_curve": self.curve.as_dict() if self.curve else None,
            "private_market": self.private_stats.as_dict() if self.private_stats else None,
            "dealer_market": self.dealer_stats.as_dict() if self.dealer_stats else None,
            "auction_floor": self.auction_floor,
            "picks": {
                "best_value": ref(self.best_value),
                "cheapest": ref(self.cheapest),
                "lowest_mileage": ref(self.lowest_mileage),
                "closest": ref(self.closest),
            },
            "undominated": [x.as_dict() for x in self.frontier],
            "dealers": [d.as_dict() for d in self.dealers],
        }


#: A retail listing priced under this fraction of the peer median is not a car
#: for sale at that price. In practice it is a deposit, a parts car, a rental
#: ad or a scam — a "$250 2022 BMW X3" on Marketplace. One of them drags the
#: reported market floor from $31,500 to $250 and flattens the price curve.
IMPLAUSIBLE_FRACTION = 0.25
#: Below this many listings there is no peer group to judge against.
MIN_FOR_PLAUSIBILITY = 4


def flag_implausible(listings: list[CarListing]) -> list[CarListing]:
    """Mark listings too cheap to be real, and return the ones worth counting.

    Marked rather than dropped: the listing still reaches the report, under a
    heading that says it was not counted, because a shopper who sees it on
    Marketplace deserves to know the engine saw it and why it was set aside.
    """
    priced = [x for x in listings if x.price is not None and x.price > 0]
    if len(priced) < MIN_FOR_PLAUSIBILITY:
        return listings
    median = statistics.median([float(x.price) for x in priced])
    floor = median * IMPLAUSIBLE_FRACTION
    kept = []
    for item in listings:
        if item.price is not None and float(item.price) < floor:
            item.extra = dict(item.extra)
            item.extra["not_counted"] = (
                f"priced far below the rest of the market (median "
                f"${median:,.0f}) — usually a deposit, a parts car or a scam")
            continue
        kept.append(item)
    return kept


def analyse(listings: list[CarListing]) -> Analysis:
    """The full deterministic read on a set of results."""
    retail = [x for x in listings if x.channel != AUCTION and x.price is not None]
    retail = flag_implausible(retail)
    stats = market_stats(retail)
    curve = fit_price_curve(retail)
    score_listings(retail, curve, stats)

    auctions = [x for x in listings if x.channel == AUCTION]
    bids = [float(x.current_bid) for x in auctions if x.current_bid]

    # Rank value only among cars scored the same way. A median-only score is
    # not comparable with a mileage-adjusted one, and mixing them handed "best
    # value" to whichever listing simply omitted its odometer.
    scored = [x for x in retail if x.deal_score is not None
              and x.extra.get("score_basis") == "mileage-adjusted"]
    if not scored:
        scored = [x for x in retail if x.deal_score is not None]
    with_mileage = [x for x in retail if x.mileage_km]
    with_distance = [x for x in retail if x.distance_km is not None]

    return Analysis(
        stats=stats,
        curve=curve,
        frontier=pareto_frontier(retail),
        dealers=dealer_postures(retail),
        best_value=max(scored, key=lambda x: x.deal_score) if scored else None,
        cheapest=min(retail, key=lambda x: float(x.price)) if retail else None,
        lowest_mileage=min(with_mileage, key=lambda x: x.mileage_km) if with_mileage else None,
        closest=min(with_distance, key=lambda x: x.distance_km) if with_distance else None,
        private_stats=market_stats([x for x in retail if x.seller_type == "private"]) or None,
        dealer_stats=market_stats([x for x in retail if x.seller_type == "dealer"]) or None,
        auction_floor=min(bids) if bids else None,
    )
