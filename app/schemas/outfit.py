"""Pydantic schemas for the manual outfit try-on endpoint."""
from pydantic import BaseModel, Field

from app.schemas.item import ItemRead


class TryOnItem(BaseModel):
    slot: str = Field(..., description="Slot outfit: atasan, bawahan, alas kaki, kacamata, topi")
    item: ItemRead


class TryOnResponse(BaseModel):
    items: list[TryOnItem] = Field(default_factory=list)
    total_price: float = Field(..., description="Total harga item, dari basis data")
    mode: str = Field(..., description="mannequin atau user_photo")
    generated_image_path: str | None = Field(None, description="Path relatif pratinjau outfit")
    generated_image_url: str | None = Field(None, description="URL lengkap pratinjau outfit")
    image_editing_error: str | None = Field(None, description="Pesan error jika pratinjau gagal")
