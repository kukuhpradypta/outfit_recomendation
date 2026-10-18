"""SQLAlchemy ORM models."""
from datetime import datetime

from sqlalchemy import String, Numeric, Text, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# Body zones used by the mannequin composite to place an item.
BODY_ZONES = ("upper", "lower", "feet", "outer", "accessory")


class Category(Base):
    __tablename__ = "category"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    # Where items of this category sit on the body (drives the outfit preview).
    body_zone: Mapped[str] = mapped_column(String(20), nullable=False, default="accessory")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    items: Mapped[list["Item"]] = relationship(back_populates="category")


class Item(Base):
    __tablename__ = "item"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category_id: Mapped[int] = mapped_column(
        ForeignKey("category.id"), nullable=False, index=True
    )
    price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    image_path: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    category: Mapped["Category"] = relationship(back_populates="items")
