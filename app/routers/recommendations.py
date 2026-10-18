"""Outfit recommendation endpoint."""
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.repositories.item_repository import ItemRepository
from app.schemas.item import ItemRead
from app.schemas.recommendation import RecommendationResponse, RecommendedItem
from app.services import storage_service
from app.services.recommendation_service import RecommendationService
from app.utils.money import MoneyParseError, parse_rupiah

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/recommendations", tags=["recommendations"])


@router.post("", response_model=RecommendationResponse)
async def create_recommendation(
    request: Request,
    image: UploadFile | None = File(
        None,
        description=(
            "Foto pengguna (jpg/png/webp). Wajib jika mode='user_photo'. "
            "Opsional jika mode='mannequin'."
        ),
    ),
    budget: str = Form(
        ...,
        description=(
            "Estimasi budget. Mendukung '500.000' / 'Rp500.000' (titik = ribuan) "
            "maupun angka polos '500000'. Koma = desimal."
        ),
    ),
    prompt: str | None = Form(
        None, description="Prompt tambahan opsional, mis. 'gaya formal, warna netral'"
    ),
    mode: str = Form(
        "mannequin",
        description="Mode visualisasi: 'mannequin' (siluet manekin) atau 'user_photo' (foto saya)",
    ),
    db: Session = Depends(get_db),
):
    """Recommend an outfit from the real catalog within budget, with outfit preview."""
    if mode not in {"mannequin", "user_photo"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="mode harus 'mannequin' atau 'user_photo'.",
        )

    try:
        budget_value = parse_rupiah(budget)
    except MoneyParseError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    # Treat an empty upload (e.g. --form 'image=') as "no image".
    has_image = image is not None and getattr(image, "filename", None)

    if mode == "user_photo" and not has_image:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Mode 'user_photo' membutuhkan field 'image'.",
        )

    # Validate and read the user photo if provided.
    user_bytes: bytes | None = None
    user_mime = "image/jpeg"
    if has_image:
        try:
            storage_service.validate_image(image)
            user_bytes = await image.read()
            if not user_bytes:
                raise ValueError("File gambar kosong.")
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
        user_mime = (image.content_type or "image/jpeg").lower()

    repo = ItemRepository(db)
    try:
        service = RecommendationService(repo)
    except RuntimeError as exc:  # e.g. missing API key
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))

    try:
        result = service.recommend(
            user_image=user_bytes,
            user_image_mime=user_mime,
            budget=budget_value,
            extra_prompt=prompt,
            mode=mode,
        )
    except Exception as exc:
        logger.exception("Recommendation failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Gagal menghasilkan rekomendasi: {exc}",
        )

    base_url = str(request.base_url)
    chosen_items = result["chosen_items"]
    reasons: dict[int, str] = result["reasons"]
    recommended = []
    for it in chosen_items:
        item_read = ItemRead.model_validate(it)
        item_read.image_url = storage_service.build_public_url(base_url, it.image_path)
        recommended.append(
            RecommendedItem(item=item_read, reason=reasons.get(it.id, "Dipilih oleh AI."))
        )
    total_price = result["total_price"]

    generated_path = result["generated_image_path"]
    return RecommendationResponse(
        items=recommended,
        total_price=total_price,
        budget=budget_value,
        within_budget=total_price <= budget_value,
        overall_reasoning=result["overall_reasoning"],
        photo_analysis=result.get("photo_analysis", ""),
        mode=mode,
        generated_image_path=generated_path,
        generated_image_url=storage_service.build_public_url(base_url, generated_path),
        image_editing_error=result["image_editing_error"],
    )
