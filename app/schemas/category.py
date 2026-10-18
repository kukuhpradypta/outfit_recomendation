"""Pydantic schemas for the Category resource."""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import BODY_ZONES


class CategoryCreate(BaseModel):
    name: str = Field(..., max_length=100, examples=["atasan"])
    body_zone: str = Field(
        "accessory",
        description=f"Zona tubuh untuk pratinjau. Salah satu dari: {', '.join(BODY_ZONES)}",
        examples=["upper"],
    )

    @field_validator("body_zone")
    @classmethod
    def _validate_zone(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in BODY_ZONES:
            raise ValueError(f"body_zone harus salah satu dari: {', '.join(BODY_ZONES)}")
        return v


class CategoryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    body_zone: str
    created_at: datetime
