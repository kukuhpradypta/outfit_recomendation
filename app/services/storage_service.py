"""Handles saving uploaded and generated images to the public/ folder."""
import os
import uuid
from pathlib import Path

from fastapi import UploadFile

from app.config import settings

ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

ITEMS_SUBDIR = Path("images") / "items"
GENERATED_SUBDIR = Path("images") / "generated"

MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB


def _public_root() -> Path:
    root = Path(settings.public_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def ensure_dirs() -> None:
    (_public_root() / ITEMS_SUBDIR).mkdir(parents=True, exist_ok=True)
    (_public_root() / GENERATED_SUBDIR).mkdir(parents=True, exist_ok=True)


def validate_image(upload: UploadFile) -> str:
    """Validate content type and return the file extension. Raises ValueError if invalid."""
    content_type = (upload.content_type or "").lower()
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise ValueError(
            f"Format gambar tidak didukung: '{content_type}'. "
            f"Gunakan salah satu dari: {', '.join(sorted(ALLOWED_IMAGE_TYPES))}"
        )
    return ALLOWED_IMAGE_TYPES[content_type]


async def read_image_upload(upload: UploadFile) -> tuple[bytes, str]:
    """Validate and read an uploaded image. Returns (bytes, mime).

    Raises ValueError with a user-facing message if the upload is invalid.
    """
    validate_image(upload)
    data = await upload.read()
    if not data:
        raise ValueError("File gambar kosong.")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Ukuran gambar melebihi batas 10 MB.")
    return data, (upload.content_type or "image/jpeg").lower()


def has_upload(upload: UploadFile | None) -> bool:
    """True if a real file was sent (an empty form field like 'image=' is not)."""
    return upload is not None and bool(getattr(upload, "filename", None))


def save_item_bytes(data: bytes, ext: str) -> str:
    """Save already-read item image bytes and return the public-relative path."""
    if len(data) == 0:
        raise ValueError("File gambar kosong.")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Ukuran gambar melebihi batas 10 MB.")
    ensure_dirs()
    filename = f"{uuid.uuid4().hex}{ext}"
    rel_path = ITEMS_SUBDIR / filename
    abs_path = _public_root() / rel_path
    with open(abs_path, "wb") as f:
        f.write(data)
    return str(rel_path).replace(os.sep, "/")


def save_generated_image(data: bytes, ext: str = ".png") -> str:
    """Save a generated image and return its path relative to public/."""
    ensure_dirs()
    filename = f"{uuid.uuid4().hex}{ext}"
    rel_path = GENERATED_SUBDIR / filename
    abs_path = _public_root() / rel_path
    with open(abs_path, "wb") as f:
        f.write(data)
    return str(rel_path).replace(os.sep, "/")


def absolute_path(relative_path: str) -> Path:
    """Resolve a public-relative path to an absolute filesystem path."""
    return _public_root() / relative_path


def read_bytes(relative_path: str) -> bytes:
    with open(absolute_path(relative_path), "rb") as f:
        return f.read()


# Static files are mounted at this prefix in main.py.
PUBLIC_URL_PREFIX = "public"


def build_public_url(base_url: str, relative_path: str | None) -> str | None:
    """Turn a public-relative path into a full URL using the request base URL.

    base_url example: "http://localhost:8000/" -> returns
    "http://localhost:8000/public/images/generated/xxx.png".
    """
    if not relative_path:
        return None
    base = base_url.rstrip("/")
    rel = relative_path.lstrip("/")
    return f"{base}/{PUBLIC_URL_PREFIX}/{rel}"
