"""Outfit slots, slot validation, and preview rendering.

Shared by the try-on endpoint (user picks items manually) and the
recommendation flow (AI picks items).
"""
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from app.models import Item
from app.services import composite_service

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SlotRule:
    field: str                       # request field name, e.g. "atasan_id"
    label: str                       # human-readable slot name
    body_zones: frozenset[str] = frozenset()      # allowed Category.body_zone
    category_names: frozenset[str] = frozenset()  # allowed Category.name

    def accepts(self, item: Item) -> bool:
        cat = item.category
        if cat is None:
            return False
        if self.category_names:
            return cat.name.strip().lower() in self.category_names
        return cat.body_zone in self.body_zones

    def describe(self) -> str:
        if self.category_names:
            return ", ".join(sorted(self.category_names))
        return f"body_zone {', '.join(sorted(self.body_zones))}"


# Slot rules for the try-on endpoint. Tops/bottoms/footwear are matched by
# body_zone so new categories (e.g. "luaran", "sneakers") work automatically;
# hats and glasses share the "accessory" zone, so they are matched by name.
TRY_ON_SLOTS: tuple[SlotRule, ...] = (
    SlotRule("atasan_id", "atasan", body_zones=frozenset({"upper", "outer"})),
    SlotRule("bawahan_id", "bawahan", body_zones=frozenset({"lower"})),
    SlotRule("alas_kaki_id", "alas kaki", body_zones=frozenset({"feet"})),
    SlotRule("kacamata_id", "kacamata", category_names=frozenset({"kacamata"})),
    SlotRule("topi_id", "topi", category_names=frozenset({"topi"})),
)


def slot_key(item: Item) -> str:
    """One item per body zone (sandals and shoes are both footwear);
    accessories are separate per category (a hat AND glasses is fine)."""
    cat = item.category
    if cat is None:
        return ""
    return f"accessory:{cat.name}" if cat.body_zone == "accessory" else cat.body_zone


def build_visual(
    items: Sequence[Item], mode: str, user_image: bytes | None
) -> tuple[str | None, str | None]:
    """Compose an outfit preview locally. Returns (relative_path, error)."""
    pieces = [
        composite_service.OutfitPiece(
            name=it.name,
            body_zone=it.category.body_zone if it.category else "accessory",
            category_name=it.category.name if it.category else "",
            image_rel_path=it.image_path,
        )
        for it in items
    ]
    try:
        return composite_service.build_outfit_image(
            pieces=pieces, mode=mode, user_image=user_image
        ), None
    except Exception as exc:
        logger.warning("Komposit outfit gagal: %s", exc)
        return None, f"Gagal membuat visual outfit: {exc}"
