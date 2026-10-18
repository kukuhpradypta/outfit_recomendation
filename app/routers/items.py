"""Item management endpoints."""
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Item
from app.repositories.category_repository import CategoryRepository
from app.repositories.item_repository import ItemRepository
from app.schemas.item import ItemRead
from app.services import storage_service
from app.utils.money import MoneyParseError, parse_rupiah

router = APIRouter(prefix="/api/v1/items", tags=["items"])


def _to_read(item: Item, request: Request) -> ItemRead:
    """Serialize an Item and attach the full image URL based on the request host."""
    read = ItemRead.model_validate(item)
    read.image_url = storage_service.build_public_url(str(request.base_url), item.image_path)
    return read


@router.post("", response_model=ItemRead, status_code=status.HTTP_201_CREATED)
async def create_item(
    request: Request,
    name: str = Form(..., description="Nama produk"),
    category_id: int = Form(..., description="ID kategori (lihat GET /api/v1/categories)"),
    price: str = Form(
        ...,
        description=(
            "Harga produk. Mendukung format Indonesia: '100.000' atau 'Rp100.000' "
            "(titik = ribuan), maupun angka polos '100000'. Koma = desimal."
        ),
    ),
    description: str | None = Form(
        None,
        description="Deskripsi produk (opsional). Sebutkan warna/detail agar AI akurat.",
    ),
    image: UploadFile = File(..., description="File gambar produk (jpg/png/webp)"),
    db: Session = Depends(get_db),
):
    """Create a new catalog item with an uploaded image."""
    # Validate price.
    try:
        price_value = parse_rupiah(price)
    except MoneyParseError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    # Validate category exists.
    category = CategoryRepository(db).get(category_id)
    if category is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"category_id {category_id} tidak ditemukan. Buat dulu via POST /api/v1/categories.",
        )

    # Validate image and read bytes.
    try:
        ext = storage_service.validate_image(image)
        image_bytes = await image.read()
        if not image_bytes:
            raise ValueError("File gambar kosong.")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    # Persist the image file.
    image_path = storage_service.save_item_bytes(image_bytes, ext)

    repo = ItemRepository(db)
    item = repo.create(
        name=name,
        category_id=category_id,
        price=price_value,
        image_path=image_path,
        description=description,
    )
    return _to_read(item, request)


@router.get("", response_model=list[ItemRead])
def list_items(request: Request, db: Session = Depends(get_db)):
    """List all catalog items."""
    return [_to_read(it, request) for it in ItemRepository(db).list_all()]


@router.get("/{item_id}", response_model=ItemRead)
def get_item(item_id: int, request: Request, db: Session = Depends(get_db)):
    item = ItemRepository(db).get(item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item tidak ditemukan.")
    return _to_read(item, request)
