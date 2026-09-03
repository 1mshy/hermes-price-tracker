"""REST surface — the same capabilities the agent gets over MCP, for humans/curl."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from . import community, fx, preferences, service
from . import scheduler as sweep_scheduler
from .notify import Alert, channel_status, dispatch
from .stores.registry import catalog_summary, fetch_offer

router = APIRouter()


class TrackUrlIn(BaseModel):
    url: str
    target_price: float | None = None
    drop_pct: float | None = Field(default=None, ge=0, le=99)
    alert_on_restock: bool = False
    label: str = ""
    channels: str = ""
    cooldown_hours: int = 12


class TrackQueryIn(BaseModel):
    description: str
    target_price: float | None = None
    drop_pct: float | None = Field(default=None, ge=0, le=99)
    alert_on_restock: bool = False
    stores: list[str] | None = None
    channels: str = ""
    cooldown_hours: int = 12


class CompareIn(BaseModel):
    query: str
    stores: list[str] | None = None
    limit_per_store: int = 3
    country: str | None = None


class CarSearchIn(BaseModel):
    """Structured car search. Give make+model+location, or `text` alone."""
    make: str = ""
    model: str = ""
    location: str = ""
    text: str = ""
    year: int | None = None
    year_min: int | None = None
    year_max: int | None = None
    radius_km: float = 150.0
    price_max: float | None = None
    price_min: float | None = None
    max_mileage_km: int | None = None
    include_auctions: bool = True
    include_salvage: bool = False
    sources: list[str] | None = None
    deep: bool = False


class TrackerPatch(BaseModel):
    target_price: float | None = None
    drop_pct: float | None = None
    alert_on_restock: bool | None = None
    channels: str | None = None
    cooldown_hours: int | None = None
    active: bool | None = None
    label: str | None = None
    reset_baseline: bool = False


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "channels": channel_status()}


@router.get("/stores")
async def stores() -> dict:
    return {"stores": catalog_summary()}


@router.get("/price")
async def price(url: str = Query(..., description="full product URL")) -> dict:
    return (await fetch_offer(url)).as_dict()


@router.post("/compare")
async def compare(body: CompareIn) -> dict:
    return await service.compare(body.query, stores=body.stores,
                                 limit_per_store=body.limit_per_store, country=body.country)


@router.get("/trackers")
async def trackers() -> dict:
    return {"trackers": await service.list_tracked()}


@router.post("/trackers/url")
async def track_url(body: TrackUrlIn) -> dict:
    result = await service.track_url(
        body.url, target_price=body.target_price, drop_pct=body.drop_pct,
        alert_on_restock=body.alert_on_restock, label=body.label, channels=body.channels,
        cooldown_hours=body.cooldown_hours)
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail=result)
    return result


@router.post("/trackers/query")
async def track_query(body: TrackQueryIn) -> dict:
    result = await service.track_query(
        body.description, stores=body.stores, target_price=body.target_price,
        drop_pct=body.drop_pct, alert_on_restock=body.alert_on_restock,
        channels=body.channels, cooldown_hours=body.cooldown_hours)
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail=result)
    return result


@router.patch("/trackers/{tracker_id}")
async def patch_tracker(tracker_id: int, body: TrackerPatch) -> dict:
    result = await service.set_tracker(tracker_id, **body.model_dump())
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result)
    return result


@router.delete("/trackers/{tracker_id}")
async def remove_tracker(tracker_id: int) -> dict:
    result = await service.delete_tracker(tracker_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result)
    return result


@router.post("/trackers/{product_id}/listings")
async def add_listing(product_id: int, url: str = Query(...)) -> dict:
    return await service.add_offer(product_id, url)


@router.post("/trackers/{product_id}/broaden")
async def broaden(product_id: int, max_new: int = 6) -> dict:
    return await service.find_more_stores(product_id, max_new=max_new)


@router.get("/history/{product_id}")
async def history(product_id: int, limit: int = 200) -> dict:
    result = await service.price_history(product_id, limit=limit)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result)
    return result


@router.get("/alerts")
async def alerts(limit: int = 30) -> dict:
    return await service.list_alerts(limit=limit)


@router.get("/community")
async def community_search(query: str = Query(..., description="product to look for"),
                           days: int = 14, limit: int = 20) -> dict:
    return await community.search(query, days=days, limit=limit)


@router.get("/community/thread")
async def community_thread(url: str = Query(..., description="reddit.com thread URL"),
                           max_comments: int = 15) -> dict:
    return await community.thread(url, max_comments=max_comments)


class ScheduleIn(BaseModel):
    cron: str


@router.get("/schedule")
async def schedule() -> dict:
    return sweep_scheduler.current()


@router.patch("/schedule")
async def set_schedule(body: ScheduleIn) -> dict:
    try:
        return {"ok": True, **sweep_scheduler.reschedule(body.cron)}
    except sweep_scheduler.ScheduleError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


class LocaleIn(BaseModel):
    currency: str
    region: str = ""


@router.get("/locale")
async def locale_preference() -> dict:
    return preferences.current()


@router.patch("/locale")
async def set_locale(body: LocaleIn) -> dict:
    try:
        return {"ok": True, **preferences.set_preference(body.currency, body.region)}
    except preferences.LocaleError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.get("/fx")
async def exchange_rates() -> dict:
    return fx.status()


@router.post("/fx/refresh")
async def refresh_exchange_rates() -> dict:
    return await fx.refresh()


@router.post("/refresh")
async def refresh() -> dict:
    return await service.refresh_offers()


@router.post("/notify/test")
async def notify_test(message: str = "Hermes Shopping test alert") -> dict:
    outcome = await dispatch(Alert(title="✅ Hermes Shopping", body=message))
    return {"channels": channel_status(), "result": outcome or "no channel configured"}


# ── used-car research ───────────────────────────────────────────────────
@router.get("/cars/sources")
async def car_sources() -> dict:
    from .cars import analyst
    from .cars.swarm import SOURCES
    return {
        "sources": [{"key": s.key, "label": s.label, "channel": s.channel,
                     "countries": list(s.countries)} for s in SOURCES],
        "analyst": analyst.llm.describe(),
    }


@router.post("/cars/search")
async def car_search(body: CarSearchIn) -> dict:
    from . import cars
    if body.text and not (body.make and body.model):
        return await cars.research_text(body.text, sources=body.sources, deep=body.deep)
    if not (body.make and body.model):
        raise HTTPException(422, "give make and model, or a free-text `text` query")
    query = await cars.spec.build(
        make=body.make, model=body.model, location=body.location,
        year=body.year, year_min=body.year_min, year_max=body.year_max,
        radius_km=body.radius_km, price_max=body.price_max, price_min=body.price_min,
        max_mileage_km=body.max_mileage_km, include_auctions=body.include_auctions,
        include_salvage=body.include_salvage)
    return await cars.research(query, sources=body.sources, deep=body.deep)
