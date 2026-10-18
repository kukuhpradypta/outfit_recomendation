"""Pydantic schemas for the recommendation flow."""
from pydantic import BaseModel, ConfigDict, Field

from app.schemas.item import ItemRead


class RecommendedItem(BaseModel):
    """An item chosen by the AI, paired with the reason it was picked."""

    model_config = ConfigDict(from_attributes=True)

    item: ItemRead
    reason: str = Field(..., description="Why this specific product was selected")


class RecommendationResponse(BaseModel):
    items: list[RecommendedItem] = Field(default_factory=list)
    total_price: float = Field(..., description="Sum of selected item prices, from the database")
    budget: float = Field(..., description="User-provided budget estimate")
    within_budget: bool = Field(..., description="True if total_price <= budget")
    overall_reasoning: str = Field(..., description="Overall explanation for the outfit")
    photo_analysis: str = Field(
        "", description="AI (Gemini Vision) analysis of the uploaded photo"
    )
    mode: str = Field("mannequin", description="Visualization mode used: mannequin or user_photo")
    generated_image_path: str | None = Field(
        None, description="Relative path of the composed outfit preview, null if it failed"
    )
    generated_image_url: str | None = Field(
        None, description="Full URL of the composed outfit preview, null if it failed"
    )
    image_editing_error: str | None = Field(
        None, description="Error message if visualization failed, otherwise null"
    )
