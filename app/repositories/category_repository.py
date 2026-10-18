"""Data-access layer for Category records."""
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Category


class CategoryRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, *, name: str, body_zone: str) -> Category:
        category = Category(name=name, body_zone=body_zone)
        self.db.add(category)
        self.db.commit()
        self.db.refresh(category)
        return category

    def get(self, category_id: int) -> Category | None:
        return self.db.get(Category, category_id)

    def get_by_name(self, name: str) -> Category | None:
        stmt = select(Category).where(Category.name == name)
        return self.db.execute(stmt).scalar_one_or_none()

    def list_all(self) -> Sequence[Category]:
        return self.db.execute(select(Category).order_by(Category.id)).scalars().all()
