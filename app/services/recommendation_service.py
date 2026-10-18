"""Orchestrates the recommendation flow.

Concerns are kept as separate methods so they can evolve independently:
  1. analyze_user_photo -> AI (Gemini Vision) reads the user/mannequin photo
  2. select_products    -> AI picks item IDs from the real catalog
  3. explain            -> AI explains the overall outfit choice
  4. build_visual       -> local Pillow composite (free, no AI image billing)

The AI is constrained to pick only from the provided catalog IDs; any
hallucinated IDs are dropped. Prices and totals always come from the DB.

Image visualization uses a free local composite (mannequin or user photo),
because Gemini image models have zero free-tier quota. Gemini is still the
"brain": it analyzes the photo, selects the outfit, and explains it.
"""
import json
import logging
from collections.abc import Sequence

from app.models import Item
from app.repositories.item_repository import ItemRepository
from app.services import composite_service
from app.services.gemini_client import get_gemini_client

logger = logging.getLogger(__name__)


class RecommendationService:
    def __init__(self, repo: ItemRepository):
        self.repo = repo
        self.gemini = get_gemini_client()

    # --------------------------------------------------------------- catalog
    @staticmethod
    def _catalog_as_text(items: Sequence[Item]) -> str:
        rows = []
        for it in items:
            rows.append(
                {
                    "id": it.id,
                    "name": it.name,
                    "category": it.category.name if it.category else "",
                    "price": float(it.price),
                    "description": it.description or "",
                }
            )
        return json.dumps(rows, ensure_ascii=False)

    # --------------------------------------------------- step 0: vision (AI)
    def analyze_user_photo(self, *, user_image: bytes, user_image_mime: str) -> str:
        """Use Gemini Vision to describe the person/mannequin for better matching.

        Best-effort: returns a short description, or empty string on failure.
        """
        prompt = (
            "Analisis foto ini untuk kebutuhan rekomendasi pakaian. Sebutkan secara "
            "singkat (1-2 kalimat) hal yang relevan: tone warna kulit, perkiraan "
            "jenis kelamin/penampilan jika terlihat, dan tone warna yang kemungkinan "
            "cocok. Jika ini mannequin, sebutkan saja. Balas HANYA JSON: "
            '{"analysis": "<deskripsi singkat>"}'
        )
        try:
            result = self.gemini.generate_json_with_image(
                prompt=prompt, image=user_image, image_mime=user_image_mime
            )
            return str(result.get("analysis", "")).strip()
        except Exception as exc:
            logger.warning("Analisis foto gagal (dilewati): %s", exc)
            return ""

    # -------------------------------------------------------- step 1: select
    def select_products(
        self,
        *,
        catalog: Sequence[Item],
        budget: float,
        extra_prompt: str | None,
        photo_analysis: str = "",
    ) -> tuple[list[int], dict[int, str]]:
        """Return chosen item IDs and a map of item_id -> reason."""
        prompt = f"""Kamu adalah asisten fashion stylist. Pilih kombinasi outfit dari KATALOG berikut.

ATURAN WAJIB:
- Hanya pilih produk yang ada di KATALOG (gunakan "id" persis seperti tertera).
- JANGAN mengarang produk, nama, atau harga.
- Total harga produk yang dipilih TIDAK BOLEH melebihi budget: {budget}.
- Usahakan membentuk satu set outfit yang padu (mis. atasan + bawahan + alas kaki jika tersedia).
- Pertimbangkan preferensi gaya pengguna dan hasil analisis foto jika ada.
- Warna produk dapat kamu simpulkan dari "name" dan "description"; pertimbangkan keselarasan warna antar item.

HASIL ANALISIS FOTO PENGGUNA (opsional): {photo_analysis or "(tidak ada)"}
PREFERENSI PENGGUNA (opsional): {extra_prompt or "(tidak ada)"}

KATALOG (JSON array): {self._catalog_as_text(catalog)}

Balas HANYA dalam format JSON dengan struktur:
{{
  "selected": [
    {{"id": <int id dari katalog>, "reason": "<alasan singkat memilih item ini>"}}
  ]
}}
"""
        try:
            result = self.gemini.generate_json(prompt)
        except Exception as exc:
            logger.warning("Pemilihan AI gagal, pakai fallback budget: %s", exc)
            return self._fallback_selection(catalog, budget)

        selected = result.get("selected", []) if isinstance(result, dict) else []

        valid_ids = {it.id for it in catalog}
        chosen_ids: list[int] = []
        reasons: dict[int, str] = {}
        for entry in selected:
            try:
                item_id = int(entry["id"])
            except (KeyError, ValueError, TypeError):
                continue
            if item_id in valid_ids and item_id not in reasons:
                chosen_ids.append(item_id)
                reasons[item_id] = str(entry.get("reason", "")).strip() or "Dipilih oleh AI."

        if not chosen_ids:
            return self._fallback_selection(catalog, budget)
        return chosen_ids, reasons

    @staticmethod
    def _fallback_selection(
        catalog: Sequence[Item], budget: float
    ) -> tuple[list[int], dict[int, str]]:
        """Deterministic one-per-category pick within budget (no AI).

        Used only when the AI call is unavailable, so the app still returns a
        usable outfit and a preview image.
        """
        chosen_ids: list[int] = []
        reasons: dict[int, str] = {}
        used_categories: set[str] = set()
        remaining = budget
        # Cheapest first so we can fit more categories.
        for it in sorted(catalog, key=lambda x: float(x.price)):
            cat = (it.category.name if it.category else "").lower()
            if cat in used_categories:
                continue
            if float(it.price) <= remaining:
                chosen_ids.append(it.id)
                reasons[it.id] = (
                    "Dipilih otomatis (layanan AI sedang sibuk) karena sesuai budget "
                    "dan melengkapi kategori outfit."
                )
                used_categories.add(cat)
                remaining -= float(it.price)
        return chosen_ids, reasons

    # ------------------------------------------------------- step 2: explain
    def explain(
        self,
        *,
        items: Sequence[Item],
        budget: float,
        total_price: float,
        extra_prompt: str | None,
        photo_analysis: str = "",
    ) -> str:
        """Produce an overall human-readable reasoning for the outfit."""
        item_summary = [
            {"name": it.name,
             "category": it.category.name if it.category else "",
             "description": it.description or "",
             "price": float(it.price)}
            for it in items
        ]
        prompt = f"""Jelaskan dalam 2-4 kalimat mengapa kombinasi outfit ini cocok untuk pengguna.

Analisis foto pengguna: {photo_analysis or "(tidak ada)"}
Preferensi pengguna: {extra_prompt or "(tidak ada)"}
Budget: {budget}
Total harga: {total_price}
Item terpilih (JSON): {json.dumps(item_summary, ensure_ascii=False)}

Balas HANYA dalam format JSON: {{"reasoning": "<penjelasan>"}}
"""
        try:
            result = self.gemini.generate_json(prompt)
            reasoning = str(result.get("reasoning", "")).strip()
            return reasoning or "Kombinasi ini dipilih agar sesuai budget dan preferensi gaya Anda."
        except Exception as exc:  # explanation is best-effort
            logger.warning("Gagal membuat penjelasan: %s", exc)
            return "Kombinasi ini dipilih agar sesuai budget dan preferensi gaya Anda."

    # --------------------------------------- step 3: visualization (free/local)
    def build_visual(
        self,
        *,
        items: Sequence[Item],
        mode: str,
        user_image: bytes | None,
    ) -> tuple[str | None, str | None]:
        """Compose an outfit preview locally (mannequin or user photo).

        Returns (generated_relative_path, error_message).
        """
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
            rel_path = composite_service.build_outfit_image(
                pieces=pieces, mode=mode, user_image=user_image
            )
            return rel_path, None
        except Exception as exc:
            logger.warning("Komposit outfit gagal: %s", exc)
            return None, f"Gagal membuat visual outfit: {exc}"

    # ------------------------------------------------------------ orchestrate
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
                "chosen_items": [],
                "reasons": {},
                "total_price": 0.0,
                "photo_analysis": "",
                "overall_reasoning": (
                    "Tidak ada produk di katalog yang sesuai dengan budget "
                    f"{budget}. Coba naikkan budget atau tambah produk."
                ),
                "generated_image_path": None,
                "image_editing_error": None,
            }

        # AI step 0: analyze the photo (multimodal) only if a photo was given.
        photo_analysis = ""
        if user_image:
            photo_analysis = self.analyze_user_photo(
                user_image=user_image, user_image_mime=user_image_mime
            )

        # AI step 1: pick products.
        chosen_ids, reasons = self.select_products(
            catalog=catalog, budget=budget, extra_prompt=extra_prompt,
            photo_analysis=photo_analysis,
        )

        # Fetch authoritative records from DB (prices come from here, not AI).
        chosen_items = list(self.repo.list_by_ids(chosen_ids))
        order = {item_id: idx for idx, item_id in enumerate(chosen_ids)}
        chosen_items.sort(key=lambda it: order.get(it.id, 0))

        total_price = round(sum(float(it.price) for it in chosen_items), 2)

        # AI step 2: explain.
        overall_reasoning = self.explain(
            items=chosen_items, budget=budget, total_price=total_price,
            extra_prompt=extra_prompt, photo_analysis=photo_analysis,
        )

        # Local step 3: build visual (free).
        generated_path, image_error = (None, None)
        if chosen_items:
            generated_path, image_error = self.build_visual(
                items=chosen_items, mode=mode, user_image=user_image,
            )

        return {
            "chosen_items": chosen_items,
            "reasons": reasons,
            "total_price": total_price,
            "photo_analysis": photo_analysis,
            "overall_reasoning": overall_reasoning,
            "generated_image_path": generated_path,
            "image_editing_error": image_error,
        }
