from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Index, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CalculationPreview(Base):
    __tablename__ = "calculation_preview"
    __table_args__ = (Index("ix_calculation_preview_user_expiry", "user_id", "expires_at"),)

    preview_id: Mapped[UUID] = mapped_column(
        primary_key=True, default=uuid4, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[str] = mapped_column(Text)
    source_plan_id: Mapped[UUID | None]
    source_plan_revision: Mapped[int | None]
    vehicle_data_version: Mapped[str] = mapped_column(Text)
    input_snapshot: Mapped[dict] = mapped_column(JSONB)
    result_payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
