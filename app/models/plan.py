from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.calculation import Calculation, Placement
    from app.models.vehicle import Vehicle


class Plan(Base):
    __tablename__ = "plan"
    __table_args__ = (
        CheckConstraint("btrim(user_id) <> ''", name="user_id_not_blank"),
        CheckConstraint("passenger_count >= 1", name="passenger_count_positive"),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint(
            "category IS NULL OR category IN ('TRAVEL', 'LEISURE', 'MOVING')", name="category_valid"
        ),
        CheckConstraint(
            "(selected_calculation_id IS NULL) = (selected_arrangement_id IS NULL)",
            name="selection_pair",
        ),
        CheckConstraint(
            "(idempotency_key IS NULL AND request_hash IS NULL) OR "
            "(idempotency_key IS NOT NULL AND request_hash IS NOT NULL "
            "AND request_hash ~ '^[0-9a-f]{64}$')",
            name="creation_request_valid",
        ),
        UniqueConstraint("user_id", "idempotency_key", name="uq_plan_user_idempotency_key"),
        Index("ix_plan_user_created", "user_id", "created_at", "plan_id"),
    )

    plan_id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[str] = mapped_column(Text)
    vehicle_id: Mapped[str] = mapped_column(
        ForeignKey("vehicle.vehicle_id", ondelete="RESTRICT"), index=True
    )
    passenger_count: Mapped[int]
    passenger_seats: Mapped[list[str] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    idempotency_key: Mapped[UUID | None]
    request_hash: Mapped[str | None] = mapped_column(Text)
    revision: Mapped[int] = mapped_column(default=1, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    selected_calculation_id: Mapped[UUID | None]
    selected_arrangement_id: Mapped[UUID | None]
    name: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    memo: Mapped[str | None] = mapped_column(Text)
    is_favorite: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    favorited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    vehicle: Mapped[Vehicle] = relationship(back_populates="plans")
    items: Mapped[list[PlanItem]] = relationship(
        back_populates="plan", order_by="PlanItem.position", passive_deletes="all"
    )
    car_seats: Mapped[list[PlanCarSeat]] = relationship(
        back_populates="plan", order_by="PlanCarSeat.seat_code", passive_deletes="all"
    )
    retained_seats: Mapped[list[PlanRetainedSeat]] = relationship(
        back_populates="plan", order_by="PlanRetainedSeat.seat_code", passive_deletes="all"
    )
    calculations: Mapped[list[Calculation]] = relationship(
        back_populates="plan",
        order_by="(Calculation.created_at, Calculation.calculation_id)",
        passive_deletes="all",
    )


class PlanItem(Base):
    __tablename__ = "plan_item"
    __table_args__ = (
        UniqueConstraint("plan_id", "item_id", name="uq_plan_item_plan_item"),
        Index(
            "uq_plan_item_active_position",
            "plan_id",
            "position",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        CheckConstraint("btrim(name) <> ''", name="name_not_blank"),
        CheckConstraint("position >= 1", name="position_positive"),
        CheckConstraint("quantity >= 1", name="quantity_positive"),
        CheckConstraint(
            "required_quantity >= 0 AND required_quantity <= quantity",
            name="required_quantity_valid",
        ),
        CheckConstraint(
            "jsonb_typeof(allowed_orientations) = 'array' AND "
            "jsonb_array_length(allowed_orientations) > 0 AND "
            'allowed_orientations <@ \'["WDH","WHD","DWH","DHW","HWD","HDW"]\'::jsonb',
            name="orientations_valid",
        ),
        CheckConstraint(
            "transport_state IN ('PACKAGED', 'ASSEMBLED', 'FOLDED', 'OTHER', 'UNKNOWN')",
            name="transport_state_valid",
        ),
        CheckConstraint(
            "dimension_source IN ('AR', 'MANUAL', 'SAVED_ITEM', 'PRODUCT_SOURCE', 'UNSPECIFIED')",
            name="dimension_source_valid",
        ),
        CheckConstraint("width_cm > 0 AND width_cm < 'Infinity'::numeric", name="width_valid"),
        CheckConstraint("depth_cm > 0 AND depth_cm < 'Infinity'::numeric", name="depth_valid"),
        CheckConstraint("height_cm > 0 AND height_cm < 'Infinity'::numeric", name="height_valid"),
    )

    item_id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    plan_id: Mapped[UUID] = mapped_column(ForeignKey("plan.plan_id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    width_cm: Mapped[Decimal] = mapped_column(Numeric)
    depth_cm: Mapped[Decimal] = mapped_column(Numeric)
    height_cm: Mapped[Decimal] = mapped_column(Numeric)
    quantity: Mapped[int]
    position: Mapped[int]
    is_active: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    required_quantity: Mapped[int] = mapped_column(default=0, server_default="0")
    allowed_orientations: Mapped[list[str]] = mapped_column(
        JSONB,
        default=lambda: ["WDH", "WHD", "DWH", "DHW", "HWD", "HDW"],
        server_default=text('\'["WDH", "WHD", "DWH", "DHW", "HWD", "HDW"]\'::jsonb'),
    )
    transport_state: Mapped[str] = mapped_column(Text, default="UNKNOWN", server_default="UNKNOWN")
    dimension_source: Mapped[str] = mapped_column(
        Text, default="UNSPECIFIED", server_default="UNSPECIFIED"
    )
    material: Mapped[str | None] = mapped_column(Text)
    saved_item_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("saved_item.saved_item_id", ondelete="SET NULL")
    )

    plan: Mapped[Plan] = relationship(back_populates="items")
    placements: Mapped[list[Placement]] = relationship(
        back_populates="item", foreign_keys="Placement.item_id", passive_deletes="all"
    )


class PlanCarSeat(Base):
    __tablename__ = "plan_car_seat"
    __table_args__ = (
        CheckConstraint(
            "seat_code IN ('ROW_1_RIGHT', 'ROW_1_CENTER', "
            "'ROW_2_LEFT', 'ROW_2_CENTER', 'ROW_2_RIGHT', "
            "'ROW_3_LEFT', 'ROW_3_CENTER', 'ROW_3_RIGHT')",
            name="seat_code_valid",
        ),
    )

    plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("plan.plan_id", ondelete="CASCADE"), primary_key=True
    )
    seat_code: Mapped[str] = mapped_column(Text, primary_key=True)

    plan: Mapped[Plan] = relationship(back_populates="car_seats")


class PlanRetainedSeat(Base):
    __tablename__ = "plan_retained_seat"
    __table_args__ = (
        CheckConstraint(
            "seat_code IN ('ROW_1_LEFT', 'ROW_1_CENTER', 'ROW_1_RIGHT', "
            "'ROW_2_LEFT', 'ROW_2_CENTER', 'ROW_2_RIGHT', "
            "'ROW_3_LEFT', 'ROW_3_CENTER', 'ROW_3_RIGHT')",
            name="seat_code_valid",
        ),
    )

    plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("plan.plan_id", ondelete="CASCADE"), primary_key=True
    )
    seat_code: Mapped[str] = mapped_column(Text, primary_key=True)

    plan: Mapped[Plan] = relationship(back_populates="retained_seats")
