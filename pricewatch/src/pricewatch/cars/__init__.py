"""Multi-source used-car search: a swarm of site scouts, then analysis.

`research()` is the whole feature in one call — fan out to every car source,
reconcile what comes back, compute the market, and render a report.
"""
from __future__ import annotations

import logging

from . import analyst, report, spec, swarm, valuation
from .spec import CarQuery
from .swarm import SwarmResult
from .valuation import Analysis

log = logging.getLogger(__name__)


async def research(query: CarQuery, *, sources: list[str] | None = None,
                   deep: bool = False, narrate: bool = True) -> dict:
    """Run the swarm and return listings, analysis and a rendered report.

    `deep` turns on the per-listing assessor agents, which read what each
    seller actually wrote. They are off by default because they are the slowest
    part of the pipeline on a self-hosted model and the report is complete
    without them.
    """
    result = await swarm.gather(query, sources=sources)
    analysis = valuation.analyse(result.listings)

    assessments: dict[int, dict] = {}
    if deep:
        assessments = await analyst.assess(result.listings)
        _attach(result, assessments)

    narrative = None
    if narrate:
        narrative = await analyst.narrate(result, analysis, assessments)

    markdown = report.render(result, analysis, narrative=narrative,
                             assessments=assessments)
    payload = result.as_dict()
    payload["analysis"] = analysis.as_dict()
    payload["report_markdown"] = markdown
    payload["analyst"] = {**analyst.llm.describe(), "narrated": bool(narrative),
                          "assessed": len(assessments)}
    return payload


def _attach(result: SwarmResult, assessments: dict[int, dict]) -> None:
    """Fold assessor output onto the listings it describes."""
    for index, data in assessments.items():
        if 0 <= index < len(result.listings):
            listing = result.listings[index]
            listing.extra = dict(listing.extra)
            for key, value in data.items():
                if value:
                    listing.extra[key] = value


async def research_text(text: str, **kwargs) -> dict:
    """Same, from a plain sentence — parses the request first."""
    fields = await analyst.parse_request(text)
    if not fields.get("make") or not fields.get("model"):
        return {"ok": False,
                "error": "could not tell which make and model you mean",
                "parsed": fields}
    query = await spec.build(
        make=fields["make"], model=fields["model"],
        location=fields.get("location") or "",
        year_min=fields.get("year_min"), year_max=fields.get("year_max"),
        radius_km=fields.get("radius_km") or 150.0,
        price_max=fields.get("price_max"), price_min=fields.get("price_min"),
        max_mileage_km=fields.get("max_mileage_km"),
        trim=fields.get("trim"),
        include_salvage=bool(fields.get("include_salvage")),
        raw=text)
    return await research(query, **kwargs)


__all__ = ["research", "research_text", "CarQuery", "SwarmResult", "Analysis",
           "spec", "swarm", "valuation", "report", "analyst"]
