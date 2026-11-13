from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Index, Numeric, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SavedItem(Base):
    __tablename__ = "saved_item"
    __table_args__ = (
        Index("ix_saved_item_user_created", "user_id", "created_at", "saved_item_id"),
        CheckConstraint("btrim(user_id) <> ''", name="user_not_blank"),
        CheckConstraint("btrim(name) <> ''", name="name_not_blank"),
        CheckConstraint("default_quantity >= 1", name="quantity_positive"),
        CheckConstraint("width_cm > 0 AND width_cm < 'Infinity'::numeric", name="width_valid"),
        CheckConstraint("depth_cm > 0 AND depth_cm < 'Infinity'::numeric", name="depth_valid"),
        CheckConstraint("height_cm > 0 AND height_cm < 'Infinity'::numeric", name="height_valid"),
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
            "dimension_source IN ('AR', 'MANUAL', 'PRODUCT_SOURCE', 'UNSPECIFIED')",
            name="dimension_source_valid",
        ),
    )

    saved_item_id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    width_cm: Mapped[Decimal] = mapped_column(Numeric)
    depth_cm: Mapped[Decimal] = mapped_column(Numeric)
    height_cm: Mapped[Decimal] = mapped_column(Numeric)
    default_quantity: Mapped[int] = mapped_column(default=1, server_default="1")
    allowed_orientations: Mapped[list[str]] = mapped_column(JSONB)
    transport_state: Mapped[str] = mapped_column(Text)
    dimension_source: Mapped[str] = mapped_column(Text)
    material: Mapped[str | None] = mapped_column(Text)
    is_favorite: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
