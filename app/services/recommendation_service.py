"""Orchestrates the recommendation flow.

Stages (kept separate so each can evolve independently):
  1. shortlist_candidates -> local, no AI: pick the most relevant items per
                             category so the prompt stays small and fast
  2. ask_ai               -> ONE Gemini call that analyses the photo (if any),
                             selects products, and explains the outfit
  3. enforce_budget       -> server-side guarantee: drop items until the total
                             fits the budget (prices always come from the DB)
  4. build_visual         -> local Pillow composite (mannequin / user photo)

Anti-hallucination: Gemini only sees real catalog IDs; unknown IDs are dropped,
and totals are recomputed from the database. If Gemini is unavailable, a
deterministic budget-based selection is used so the API still responds.
"""
import logging
import re
from collections import defaultdict
from collections.abc import Sequence

from app.config import settings
from app.models import Item
from app.repositories.item_repository import ItemRepository
from app.services import composite_service
from app.services.gemini_client import get_gemini_client

logger = logging.getLogger(__name__)

# Boilerplate text that adds tokens but no information for the model.
_NOISE_PATTERNS = [
    re.compile(r"harga merupakan estimasi[^.]*\.?", re.IGNORECASE),
]

# Occasion/style words in the user prompt -> words that appear in item text.
_STYLE_SYNONYMS = {
    "formal": ["formal", "kemeja", "shirt", "trousers", "pantofel", "loafer", "formal shoes"],
    "wawancara": ["formal", "kemeja", "shirt", "trousers"],
    "kondangan": ["formal", "ethnic", "party", "kemeja"],
    "pesta": ["party", "formal"],
    "party": ["party", "formal"],
    "hangout": ["casual", "tshirt", "jeans", "sneakers", "cap", "sunglasses"],
    "santai": ["casual", "tshirt", "shorts", "sandal", "flip"],
    "kasual": ["casual", "tshirt", "jeans", "sneakers"],
    "casual": ["casual", "tshirt", "jeans", "sneakers"],
    "cool": ["casual", "sunglasses", "cap", "sneakers", "jeans"],
    "keren": ["casual", "sunglasses", "cap", "sneakers"],
    "streetwear": ["casual", "sneakers", "cap", "tshirt", "jeans"],
    "olahraga": ["sports", "sports shoes", "track", "shorts"],
    "sport": ["sports", "sports shoes", "track"],
    "kuliah": ["casual", "shirt", "jeans", "sneakers"],
    "travel": ["travel", "casual", "sandal"],
}
_WORD_RE = re.compile(r"[a-zA-Z]+")


def _clean_description(text: str | None) -> str:
    text = text or ""
    for pat in _NOISE_PATTERNS:
        text = pat.sub("", text)
    return " ".join(text.split())[:160]


def _query_terms(extra_prompt: str | None) -> set[str]:
    words = {w.lower() for w in _WORD_RE.findall(extra_prompt or "") if len(w) > 2}
    terms = set(words)
    for w in words:
        terms.update(_STYLE_SYNONYMS.get(w, []))
    return terms


def _relevance(item: Item, terms: set[str]) -> int:
    if not terms:
        return 0
    text = f"{item.name} {item.description or ''}".lower()
    return sum(1 for t in terms if t in text)


def _slot(item: Item) -> str:
    """Outfit slot: one item per body zone (e.g. sandals and shoes are both
    footwear). Accessories are distinct per category (a hat AND glasses)."""
    cat = item.category
    if cat is None:
        return ""
    return f"accessory:{cat.name}" if cat.body_zone == "accessory" else cat.body_zone


def _spread_by_price(items: list[Item], n: int) -> list[Item]:
    """Pick n items spread evenly across the price range (cheap to pricey)."""
    if len(items) <= n:
        return items
    items = sorted(items, key=lambda it: float(it.price))
    step = (len(items) - 1) / (n - 1) if n > 1 else 0
    return [items[round(i * step)] for i in range(n)]


class RecommendationService:
    def __init__(self, repo: ItemRepository):
        self.repo = repo
        self.gemini = get_gemini_client()

    # ------------------------------------------------ stage 1: shortlist
    @staticmethod
    def shortlist_candidates(
        catalog: Sequence[Item], extra_prompt: str | None, per_category: int
    ) -> list[Item]:
        """Keep the most relevant items per category for the AI prompt.

        Items are ranked by keyword relevance to the user's prompt; within the
        same relevance the shortlist is spread across the price range so the
        AI still has cheap and premium options to fit the budget.
        """
        terms = _query_terms(extra_prompt)
        by_cat: dict[str, list[Item]] = defaultdict(list)
        for it in catalog:
            by_cat[it.category.name if it.category else ""].append(it)

        shortlist: list[Item] = []
        for items in by_cat.values():
            tiers: dict[int, list[Item]] = defaultdict(list)
            for it in items:
                tiers[_relevance(it, terms)].append(it)
            picked: list[Item] = []
            for score in sorted(tiers, reverse=True):
                room = per_category - len(picked)
                if room <= 0:
                    break
                picked.extend(_spread_by_price(tiers[score], room))
            shortlist.extend(picked)
        return shortlist

    @staticmethod
    def _catalog_as_text(items: Sequence[Item]) -> str:
        """Compact one-line-per-item format (much smaller than JSON)."""
        lines = ["id|kategori|harga|nama|deskripsi"]
        for it in items:
            cat = it.category.name if it.category else ""
            lines.append(
                f"{it.id}|{cat}|{int(float(it.price))}|{it.name}|{_clean_description(it.description)}"
            )
        return "\n".join(lines)

    # --------------------------------------------------- stage 2: ask AI
    def ask_ai(
        self,
        *,
        candidates: Sequence[Item],
        budget: float,
        extra_prompt: str | None,
        user_image: bytes | None,
        user_image_mime: str,
    ) -> dict:
        """Single Gemini call: analyse photo (if any), select items, explain."""
        photo_rule = (
            '- Analisis foto pengguna terlampir (tone kulit, penampilan) dan isi "photo_analysis" 1 kalimat.\n'
            if user_image else
            '- Tidak ada foto; isi "photo_analysis" dengan string kosong.\n'
        )
        prompt = f"""Kamu fashion stylist. Pilih SATU set outfit dari KATALOG.

ATURAN:
- Hanya pakai "id" yang ada di KATALOG. Jangan mengarang produk atau harga.
- Total harga TIDAK BOLEH melebihi budget {int(budget)}.
- Pilih 1 atasan, 1 bawahan, dan 1 alas kaki (sepatu ATAU sendal, bukan keduanya); topi/kacamata opsional.
- Sesuaikan dengan preferensi pengguna; warna dapat disimpulkan dari nama/deskripsi.
{photo_rule}
PREFERENSI PENGGUNA: {extra_prompt or "(tidak ada)"}

KATALOG:
{self._catalog_as_text(candidates)}

Balas HANYA JSON:
{{"photo_analysis": "...", "selected": [{{"id": <int>, "reason": "<alasan singkat>"}}], "overall_reasoning": "<2-3 kalimat>"}}"""
        if user_image:
            return self.gemini.generate_json_with_image(
                prompt=prompt, image=user_image, image_mime=user_image_mime,
                max_output_tokens=1024,
            )
        return self.gemini.generate_json(prompt, max_output_tokens=1024)

    @staticmethod
    def _parse_selection(
        result: dict, valid: dict[int, Item]
    ) -> tuple[list[int], dict[int, str]]:
        chosen: list[int] = []
        reasons: dict[int, str] = {}
        used_slots: set[str] = set()
        for entry in result.get("selected", []) if isinstance(result, dict) else []:
            try:
                item_id = int(entry["id"])
            except (KeyError, ValueError, TypeError):
                continue
            item = valid.get(item_id)
            if item is None or item_id in reasons:
                continue  # hallucinated or duplicate id
            slot = _slot(item)
            if slot in used_slots:
                continue  # one item per outfit slot
            used_slots.add(slot)
            chosen.append(item_id)
            reasons[item_id] = str(entry.get("reason", "")).strip() or "Dipilih oleh AI."
        return chosen, reasons

    @staticmethod
    def _fallback_selection(
        catalog: Sequence[Item], budget: float
    ) -> tuple[list[int], dict[int, str]]:
        """Deterministic one-per-slot pick within budget (no AI)."""
        chosen: list[int] = []
        reasons: dict[int, str] = {}
        used: set[str] = set()
        remaining = budget
        for it in sorted(catalog, key=lambda x: float(x.price)):
            slot = _slot(it)
            if slot in used or float(it.price) > remaining:
                continue
            chosen.append(it.id)
            reasons[it.id] = (
                "Dipilih otomatis (layanan AI sedang sibuk) karena sesuai budget "
                "dan melengkapi kategori outfit."
            )
            used.add(slot)
            remaining -= float(it.price)
        return chosen, reasons

    # ---------------------------------------------- stage 3: enforce budget
    @staticmethod
    def enforce_budget(items: list[Item], budget: float) -> list[Item]:
        """Drop accessories first, then the priciest items, until within budget."""
        priority = {"upper": 0, "outer": 1, "lower": 2, "feet": 3, "accessory": 4}
        kept = list(items)
        while kept and sum(float(i.price) for i in kept) > budget:
            drop = max(
                kept,
                key=lambda i: (priority.get(i.category.body_zone if i.category else "", 5),
                               float(i.price)),
            )
            kept.remove(drop)
        return kept

    @staticmethod
    def _default_reasoning(items: Sequence[Item], extra_prompt: str | None) -> str:
        names = ", ".join(it.name for it in items)
        style = f" untuk kebutuhan '{extra_prompt}'" if extra_prompt else ""
        return f"Kombinasi {names} dipilih{style} dan tetap sesuai budget Anda."

    # -------------------------------------------- stage 4: visualization
    def build_visual(
        self, *, items: Sequence[Item], mode: str, user_image: bytes | None,
    ) -> tuple[str | None, str | None]:
        """Compose an outfit preview locally. Returns (path, error)."""
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

    # ------------------------------------------------------- orchestrate
    def recommend(
        self,
        *,
        user_image: bytes | None,
        user_image_mime: str,
        budget: float,
        extra_prompt: str | None,
        mode: str = "mannequin",
    ) -> dict:
        catalog = self.repo.list_within_budget(budget)
        if not catalog:
            return {
                "chosen_items": [], "reasons": {}, "total_price": 0.0,
                "photo_analysis": "",
                "overall_reasoning": (
                    "Tidak ada produk di katalog yang sesuai dengan budget "
                    f"{budget}. Coba naikkan budget atau tambah produk."
                ),
                "generated_image_path": None, "image_editing_error": None,
            }

        candidates = self.shortlist_candidates(
            catalog, extra_prompt, settings.reco_max_per_category
        )
        valid = {it.id: it for it in candidates}

        photo_analysis, overall = "", ""
        try:
            result = self.ask_ai(
                candidates=candidates, budget=budget, extra_prompt=extra_prompt,
                user_image=user_image, user_image_mime=user_image_mime,
            )
            chosen_ids, reasons = self._parse_selection(result, valid)
            photo_analysis = str(result.get("photo_analysis", "") or "").strip()
            overall = str(result.get("overall_reasoning", "") or "").strip()
        except Exception as exc:
            logger.warning("Gemini gagal, pakai fallback budget: %s", exc)
            chosen_ids, reasons = [], {}

        if not chosen_ids:
            chosen_ids, reasons = self._fallback_selection(catalog, budget)
            valid = {it.id: it for it in catalog}
            overall = ""

        # Prices come from the DB records, never from the AI output.
        chosen_items = self.enforce_budget([valid[i] for i in chosen_ids], budget)
        total_price = round(sum(float(it.price) for it in chosen_items), 2)

        generated_path, image_error = None, None
        if chosen_items:
            generated_path, image_error = self.build_visual(
                items=chosen_items, mode=mode, user_image=user_image,
            )

        return {
            "chosen_items": chosen_items,
            "reasons": reasons,
            "total_price": total_price,
            "photo_analysis": photo_analysis,
            "overall_reasoning": overall or self._default_reasoning(chosen_items, extra_prompt),
            "generated_image_path": generated_path,
            "image_editing_error": image_error,
        }
