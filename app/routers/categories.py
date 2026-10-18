"""Category management endpoints."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.repositories.category_repository import CategoryRepository
from app.schemas.category import CategoryCreate, CategoryRead

router = APIRouter(prefix="/api/v1/categories", tags=["categories"])


@router.post("", response_model=CategoryRead, status_code=status.HTTP_201_CREATED)
def create_category(payload: CategoryCreate, db: Session = Depends(get_db)):
    """Create a new category. `name` must be unique."""
    repo = CategoryRepository(db)
    name = payload.name.strip()
    if repo.get_by_name(name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Kategori '{name}' sudah ada.",
        )
    return repo.create(name=name, body_zone=payload.body_zone)


@router.get("", response_model=list[CategoryRead])
def list_categories(db: Session = Depends(get_db)):
    """List all categories."""
    return list(CategoryRepository(db).list_all())
