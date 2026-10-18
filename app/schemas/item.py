"""Pydantic schemas for the Item resource."""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.category import CategoryRead


class ItemBase(BaseModel):
    name: str = Field(..., max_length=255, examples=["Kemeja Linen Putih"])
    price: float = Field(..., ge=0, examples=[150000])
    description: str | None = Field(
        None,
        examples=["Kemeja linen putih, cocok untuk acara formal"],
        description="Deskripsi produk. Sebutkan warna/detail di sini agar AI akurat.",
    )


class ItemRead(ItemBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category: CategoryRead
    image_path: str = Field(..., description="Path relatif gambar di public/")
    image_url: str | None = Field(
        None, description="URL lengkap gambar produk (diisi backend)"
    )
    created_at: datetime
