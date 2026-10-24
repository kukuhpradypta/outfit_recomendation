"""Outfit recommendation endpoint."""
import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.database import get_db
from app.repositories.item_repository import ItemRepository
from app.schemas.item import ItemRead
from app.schemas.recommendation import RecommendationResponse, RecommendedItem
from app.services import storage_service
from app.services.recommendation_service import RecommendationService
from app.utils.money import MoneyParseError, parse_rupiah

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/recommendations", tags=["recommendations"])

_MAX_PREVIOUS_COMBINATIONS = 20


def _parse_combinations(raw: list[str] | None) -> list[frozenset[int]]:
    """Parse ['12,45,78', '13,45,80'] into id sets. Empty entries are ignored.

    A single field may also hold several combinations separated by ';'.
    (Entries of one ID each are interpreted by the service, which knows how
    many product types were requested.)
    """
    combos: list[frozenset[int]] = []
    for entry in raw or []:
        for part in entry.split(";"):
            part = part.strip()
            if not part:
                continue
            try:
                ids = frozenset(int(x) for x in part.split(",") if x.strip())
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"previous_combinations tidak valid: '{part}'. Gunakan format '12,45,78'.",
                )
            if ids:
                combos.append(ids)
    if len(combos) > _MAX_PREVIOUS_COMBINATIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Maksimal {_MAX_PREVIOUS_COMBINATIONS} previous_combinations.",
        )
    return combos


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
    refresh: bool = Form(
        False,
        description=(
            "true jika pengguna ingin opsi lain. Kandidat diacak dan AI diminta memberi "
            "kombinasi berbeda."
        ),
    ),
    previous_combinations: list[str] | None = Form(
        None,
        description=(
            "Kombinasi yang sudah pernah ditampilkan, format ID dipisah koma, mis. "
            "'12,45,78' (ambil dari field 'combination' respons sebelumnya). Ulangi field "
            "ini untuk beberapa kombinasi. Hasil dijamin tidak sama persis dengan kombinasi ini."
        ),
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

    excluded = _parse_combinations(previous_combinations)

    # Treat an empty upload (e.g. --form 'image=') as "no image".
    user_bytes: bytes | None = None
    user_mime = "image/jpeg"
    if storage_service.has_upload(image):
        try:
            user_bytes, user_mime = await storage_service.read_image_upload(image)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    elif mode == "user_photo":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Mode 'user_photo' membutuhkan field 'image'.",
        )

    repo = ItemRepository(db)
    try:
        service = RecommendationService(repo)
    except RuntimeError as exc:  # e.g. missing API key
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))

    try:
        # The Gemini call and image work are blocking; run them in a worker
        # thread so other customers' requests are not stuck behind this one.
        result = await run_in_threadpool(
            service.recommend,
            user_image=user_bytes,
            user_image_mime=user_mime,
            budget=budget_value,
            extra_prompt=prompt,
            mode=mode,
            refresh=refresh,
            previous_combinations=excluded,
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
        combination=result["combination"],
        notice=result["notice"],
        generated_image_path=generated_path,
        generated_image_url=storage_service.build_public_url(base_url, generated_path),
        image_editing_error=result["image_editing_error"],
    )
