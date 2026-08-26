"""REST surface — the same capabilities the agent gets over MCP, for humans/curl."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from . import service
from .notify import Alert, channel_status, dispatch
from .stores.registry import catalog_summary, fetch_offer

router = APIRouter()


class TrackUrlIn(BaseModel):
    url: str
    target_price: float | None = None
    drop_pct: float | None = Field(default=None, ge=0, le=99)
    label: str = ""
    channels: str = ""
    cooldown_hours: int = 12


class TrackQueryIn(BaseModel):
    description: str
    target_price: float | None = None
    drop_pct: float | None = Field(default=None, ge=0, le=99)
    stores: list[str] | None = None
    channels: str = ""
    cooldown_hours: int = 12


class CompareIn(BaseModel):
    query: str
    stores: list[str] | None = None
    limit_per_store: int = 3


class TrackerPatch(BaseModel):
    target_price: float | None = None
    drop_pct: float | None = None
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
                                 limit_per_store=body.limit_per_store)


@router.get("/trackers")
async def trackers() -> dict:
    return {"trackers": await service.list_tracked()}


@router.post("/trackers/url")
async def track_url(body: TrackUrlIn) -> dict:
    result = await service.track_url(
        body.url, target_price=body.target_price, drop_pct=body.drop_pct,
        label=body.label, channels=body.channels, cooldown_hours=body.cooldown_hours)
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail=result)
    return result


@router.post("/trackers/query")
async def track_query(body: TrackQueryIn) -> dict:
    result = await service.track_query(
        body.description, stores=body.stores, target_price=body.target_price,
        drop_pct=body.drop_pct, channels=body.channels, cooldown_hours=body.cooldown_hours)
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


@router.post("/refresh")
async def refresh() -> dict:
    return await service.refresh_offers()


@router.post("/notify/test")
async def notify_test(message: str = "Hermes Shopping test alert") -> dict:
    outcome = await dispatch(Alert(title="✅ Hermes Shopping", body=message))
    return {"channels": channel_status(), "result": outcome or "no channel configured"}
