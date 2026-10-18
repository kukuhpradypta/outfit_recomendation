"""Data-access layer for Item records."""
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import Item


class ItemRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        *,
        name: str,
        category_id: int,
        price: float,
        image_path: str,
        description: str | None = None,
    ) -> Item:
        item = Item(
            name=name,
            category_id=category_id,
            price=price,
            image_path=image_path,
            description=description,
        )
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        # Ensure the related category is loaded for the response.
        _ = item.category
        return item

    def get(self, item_id: int) -> Item | None:
        stmt = (
            select(Item)
            .options(joinedload(Item.category))
            .where(Item.id == item_id)
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def list_all(self) -> Sequence[Item]:
        stmt = select(Item).options(joinedload(Item.category)).order_by(Item.id)
        return self.db.execute(stmt).scalars().all()

    def list_by_ids(self, ids: Sequence[int]) -> Sequence[Item]:
        if not ids:
            return []
        stmt = select(Item).options(joinedload(Item.category)).where(Item.id.in_(ids))
        return self.db.execute(stmt).scalars().all()

    def list_within_budget(self, budget: float) -> Sequence[Item]:
        stmt = (
            select(Item)
            .options(joinedload(Item.category))
            .where(Item.price <= budget)
            .order_by(Item.category_id, Item.price)
        )
        return self.db.execute(stmt).scalars().all()
