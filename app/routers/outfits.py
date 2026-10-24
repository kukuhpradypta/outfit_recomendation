"""Manual outfit try-on: the user picks items per slot and gets a preview."""
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.database import get_db
from app.repositories.item_repository import ItemRepository
from app.schemas.item import ItemRead
from app.schemas.outfit import TryOnItem, TryOnResponse
from app.services import outfit_service, storage_service

router = APIRouter(prefix="/api/v1/outfits", tags=["outfits"])


@router.post("/try-on", response_model=TryOnResponse)
async def try_on_outfit(
    request: Request,
    atasan_id: int | None = Form(None, description="ID item atasan (kategori atasan/luaran)"),
    bawahan_id: int | None = Form(None, description="ID item bawahan"),
    alas_kaki_id: int | None = Form(None, description="ID item alas kaki (kategori sepatu atau sendal)"),
    kacamata_id: int | None = Form(None, description="ID item kacamata"),
    topi_id: int | None = Form(None, description="ID item topi"),
    mode: str = Form(
        "mannequin",
        description="Mode visualisasi: 'mannequin' atau 'user_photo' (wajib kirim 'image')",
    ),
    image: UploadFile | None = File(
        None, description="Foto pengguna (jpg/png/webp). Wajib jika mode='user_photo'."
    ),
    db: Session = Depends(get_db),
):
    """Pratinjau outfit dari item pilihan pengguna, dengan validasi kategori per slot.

    Minimal satu ID item wajib diisi. Setiap ID harus ada di katalog dan
    kategorinya harus sesuai slot (mis. kacamata_id harus item kategori kacamata).
    """
    if mode not in {"mannequin", "user_photo"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="mode harus 'mannequin' atau 'user_photo'.")

    requested = {
        "atasan_id": atasan_id, "bawahan_id": bawahan_id, "alas_kaki_id": alas_kaki_id,
        "kacamata_id": kacamata_id, "topi_id": topi_id,
    }
    if all(v is None for v in requested.values()):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Isi minimal satu item: atasan_id, bawahan_id, alas_kaki_id, kacamata_id, atau topi_id.",
        )

    repo = ItemRepository(db)
    ids = [v for v in requested.values() if v is not None]
    found = {it.id: it for it in repo.list_by_ids(ids)}

    # Validate every slot and report all problems at once.
    errors: list[dict] = []
    chosen: list[tuple[str, object]] = []
    for rule in outfit_service.TRY_ON_SLOTS:
        item_id = requested[rule.field]
        if item_id is None:
            continue
        item = found.get(item_id)
        if item is None:
            errors.append({"field": rule.field, "message": f"Item dengan ID {item_id} tidak ditemukan."})
        elif not rule.accepts(item):
            cat = item.category.name if item.category else "-"
            errors.append({
                "field": rule.field,
                "message": (
                    f"Item {item_id} ('{item.name}') berkategori '{cat}', bukan {rule.label}. "
                    f"Kategori yang diterima: {rule.describe()}."
                ),
            })
        else:
            chosen.append((rule.label, item))
    if errors:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"message": "Validasi outfit gagal.", "errors": errors})

    user_bytes: bytes | None = None
    if storage_service.has_upload(image):
        try:
            user_bytes, _ = await storage_service.read_image_upload(image)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    elif mode == "user_photo":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Mode 'user_photo' membutuhkan field 'image'.")

    items = [item for _, item in chosen]
    generated_path, image_error = await run_in_threadpool(
        outfit_service.build_visual, items, mode, user_bytes
    )

    base_url = str(request.base_url)
    response_items = []
    for label, item in chosen:
        read = ItemRead.model_validate(item)
        read.image_url = storage_service.build_public_url(base_url, item.image_path)
        response_items.append(TryOnItem(slot=label, item=read))

    return TryOnResponse(
        items=response_items,
        total_price=round(sum(float(it.price) for it in items), 2),
        mode=mode,
        generated_image_path=generated_path,
        generated_image_url=storage_service.build_public_url(base_url, generated_path),
        image_editing_error=image_error,
    )
