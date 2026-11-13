from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.plan import Plan, PlanItem


class Calculation(Base):
    __tablename__ = "calculation"
    __table_args__ = (
        UniqueConstraint("plan_id", "calculation_id", name="uq_calculation_plan_calculation"),
        Index("ix_calculation_plan_created", "plan_id", "created_at", "calculation_id"),
        CheckConstraint(
            "jsonb_typeof(current_folded_segments) = 'array' AND NOT "
            """jsonb_path_exists(current_folded_segments, '$[*] ? (@.type() != "string")')""",
            name="current_folded_segments_array",
        ),
    )

    calculation_id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    plan_id: Mapped[UUID] = mapped_column(ForeignKey("plan.plan_id", ondelete="CASCADE"))
    is_success: Mapped[bool]
    plan_revision: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    result_payload: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    # None은 현재 상태 미확인, []는 모든 구간이 펴져 있음을 확인한 상태입니다.
    current_folded_segments: Mapped[list[str] | None] = mapped_column(
        MutableList.as_mutable(JSONB(none_as_null=True))
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    plan: Mapped[Plan] = relationship(back_populates="calculations")
    arrangements: Mapped[list[Arrangement]] = relationship(
        back_populates="calculation", order_by="Arrangement.rank", passive_deletes="all"
    )


class Arrangement(Base):
    __tablename__ = "arrangement"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "calculation_id"],
            ["calculation.plan_id", "calculation.calculation_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("plan_id", "arrangement_id", name="uq_arrangement_plan_arrangement"),
        UniqueConstraint("calculation_id", "rank", name="uq_arrangement_calculation_rank"),
        CheckConstraint("rank >= 1", name="rank_positive"),
        CheckConstraint(
            """jsonb_typeof(passenger_seats) = 'array' AND """
            """passenger_seats @> '["ROW_1_LEFT"]'::jsonb AND """
            """passenger_seats <@ '["ROW_1_LEFT", "ROW_1_CENTER", "ROW_1_RIGHT", """
            """"ROW_2_LEFT", "ROW_2_CENTER", "ROW_2_RIGHT", """
            """"ROW_3_LEFT", "ROW_3_CENTER", "ROW_3_RIGHT"]'::jsonb""",
            name="passenger_seats_valid",
        ),
        CheckConstraint(
            "jsonb_typeof(target_folded_segments) = 'array' AND NOT "
            """jsonb_path_exists(target_folded_segments, '$[*] ? (@.type() != "string")')""",
            name="target_folded_segments_array",
        ),
        CheckConstraint(
            "score > '-Infinity'::numeric AND score < 'Infinity'::numeric", name="score_finite"
        ),
    )

    arrangement_id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    # 복합 외래 키로 계산과 짐이 같은 계획에 속하도록 제한합니다.
    plan_id: Mapped[UUID]
    calculation_id: Mapped[UUID]
    rank: Mapped[int]
    passenger_seats: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSONB))
    target_folded_segments: Mapped[list[str]] = mapped_column(
        MutableList.as_mutable(JSONB), default=list, server_default=text("'[]'::jsonb")
    )
    score: Mapped[Decimal | None] = mapped_column(Numeric)

    calculation: Mapped[Calculation] = relationship(back_populates="arrangements")
    placements: Mapped[list[Placement]] = relationship(
        back_populates="arrangement", order_by="Placement.load_order", passive_deletes="all"
    )


class Placement(Base):
    __tablename__ = "placement"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "arrangement_id"],
            ["arrangement.plan_id", "arrangement.arrangement_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["plan_id", "item_id"],
            ["plan_item.plan_id", "plan_item.item_id"],
            # 계산 이력에서 사용하는 짐의 개별 삭제를 막습니다.
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint(
            "arrangement_id", "load_order", name="uq_placement_arrangement_load_order"
        ),
        Index("ix_placement_plan_item", "plan_id", "item_id"),
        CheckConstraint("load_order >= 1", name="load_order_positive"),
        CheckConstraint(
            "orientation IN ('WDH', 'WHD', 'DWH', 'DHW', 'HWD', 'HDW')", name="orientation_valid"
        ),
        CheckConstraint("x_cm >= 0 AND x_cm < 'Infinity'::numeric", name="x_valid"),
        CheckConstraint("y_cm >= 0 AND y_cm < 'Infinity'::numeric", name="y_valid"),
        CheckConstraint("z_cm >= 0 AND z_cm < 'Infinity'::numeric", name="z_valid"),
    )

    placement_id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    plan_id: Mapped[UUID]
    arrangement_id: Mapped[UUID]
    item_id: Mapped[UUID]
    x_cm: Mapped[Decimal] = mapped_column(Numeric)
    y_cm: Mapped[Decimal] = mapped_column(Numeric)
    z_cm: Mapped[Decimal] = mapped_column(Numeric)
    orientation: Mapped[str] = mapped_column(Text)
    load_order: Mapped[int]

    arrangement: Mapped[Arrangement] = relationship(back_populates="placements")
    item: Mapped[PlanItem] = relationship(
        back_populates="placements", foreign_keys="Placement.item_id"
    )
