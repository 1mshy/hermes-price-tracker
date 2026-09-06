"""SQLAlchemy schema. One product ⇒ many store offers ⇒ many price points."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class Product(Base):
    """A real-world thing the user cares about, independent of where it is sold."""
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    brand: Mapped[str | None] = mapped_column(String(200), nullable=True)
    model_number: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # normalised token blob used for cross-store fuzzy matching
    match_key: Mapped[str] = mapped_column(String(500), default="", index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    offers: Mapped[list["Offer"]] = relationship(back_populates="product", cascade="all, delete-orphan")
    trackers: Mapped[list["Tracker"]] = relationship(back_populates="product", cascade="all, delete-orphan")


class Offer(Base):
    """The same product at one specific store URL."""
    __tablename__ = "offers"
    __table_args__ = (UniqueConstraint("url", name="uq_offer_url"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    store: Mapped[str] = mapped_column(String(100), index=True)
    url: Mapped[str] = mapped_column(String(1000))
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sku: Mapped[str | None] = mapped_column(String(200), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="USD")

    last_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    lowest_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    in_stock: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # When in_stock last flipped, and when it last went False→True. A first
    # read (None→True) is not a restock: nothing was ever seen missing.
    stock_changed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_restocked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    method: Mapped[str | None] = mapped_column(String(50), nullable=True)   # how we read it
    # What last_price rests on when it is not the plain sticker: an Amazon
    # clip coupon (last_price is the after-coupon figure), a time-boxed deal
    # badge. Rewritten on every successful read; None when there is nothing
    # to say. Goes out with the alert so "CA$49.99" arrives with "clip the
    # coupon" beside it.
    deal_note: Mapped[str | None] = mapped_column(String(400), nullable=True)
    last_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    product: Mapped[Product] = relationship(back_populates="offers")
    points: Mapped[list["PricePoint"]] = relationship(back_populates="offer", cascade="all, delete-orphan")


class PricePoint(Base):
    """Append-only price history; one row per observed change."""
    __tablename__ = "price_points"
    __table_args__ = (Index("ix_pp_offer_time", "offer_id", "observed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id", ondelete="CASCADE"), index=True)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    in_stock: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    observed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    offer: Mapped[Offer] = relationship(back_populates="points")


class Tracker(Base):
    """A rule: tell me when this product goes below X, drops Y%, or is back in stock."""
    __tablename__ = "trackers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(300), default="")

    target_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    drop_pct: Mapped[float | None] = mapped_column(nullable=True)
    # price the percentage rule is measured against; set on creation, refreshed upward only
    baseline_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    # Currency the thresholds are written in — that of the listing the watch was
    # created from. Listings in any other currency stay on the watch for
    # reference but are never measured against these numbers: USD 99.99 is not
    # "below" a CAD 119 baseline, whatever the digits say.
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    # Restock alerts are event-based, not threshold-based: one per out→in
    # transition, so the watermark is the last restock told, not a cooldown.
    alert_on_restock: Mapped[bool] = mapped_column(Boolean, default=False)
    last_restock_notified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    channels: Mapped[str] = mapped_column(String(200), default="")   # "discord,signal" — blank = all configured
    cooldown_hours: Mapped[int] = mapped_column(Integer, default=12)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_notified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_notified_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    product: Mapped[Product] = relationship(back_populates="trackers")


class Setting(Base):
    """Tiny KV store for engine settings adjustable at runtime (sweep cron…)."""
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(String(500))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tracker_id: Mapped[int] = mapped_column(ForeignKey("trackers.id", ondelete="CASCADE"), index=True)
    offer_id: Mapped[int | None] = mapped_column(ForeignKey("offers.id", ondelete="SET NULL"), nullable=True)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    reason: Mapped[str] = mapped_column(String(300))
    channels: Mapped[str] = mapped_column(String(200), default="")
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
