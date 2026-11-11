from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.models.plan import Plan


class Vehicle(Base):
    __tablename__ = "vehicle"
    __table_args__ = (
        CheckConstraint("btrim(vehicle_id) <> ''", name="vehicle_id_not_blank"),
        CheckConstraint("btrim(name) <> ''", name="name_not_blank"),
    )

    vehicle_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text)

    plans: Mapped[list[Plan]] = relationship(back_populates="vehicle", passive_deletes="all")
