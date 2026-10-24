"""Orchestrates the recommendation flow.

Stages (kept separate so each can evolve independently):
  1. shortlist_candidates -> local, no AI: pick the most relevant items per
                             category so the prompt stays small and fast
  2. ask_ai               -> ONE Gemini call that analyses the photo (if any),
                             selects products, and explains the outfit
  3. enforce_budget       -> server-side guarantee: drop items until the total
                             fits the budget (prices always come from the DB)
  4. ensure_new_combination -> on refresh, guarantee the outfit differs from
                             every combination the user already saw
  5. build_visual         -> local Pillow composite (mannequin / user photo)

Anti-hallucination: Gemini only sees real catalog IDs; unknown IDs are dropped,
and totals are recomputed from the database. If Gemini is unavailable, a
deterministic budget-based selection is used so the API still responds.
"""
import logging
import random
import re
from collections import defaultdict
from collections.abc import Sequence

from app.config import settings
from app.models import Item
from app.repositories.item_repository import ItemRepository
from app.services import outfit_service
from app.services.gemini_client import get_gemini_client
from app.services.outfit_service import slot_key

logger = logging.getLogger(__name__)

Combination = frozenset[int]

# Boilerplate text that adds tokens but no information for the model.
_NOISE_PATTERNS = [
    re.compile(r"harga (merupakan )?estimasi[^.]*\.?", re.IGNORECASE),
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

# Prompt words -> gender used in the dataset descriptions.
_GENDER_WORDS = {
    "pria": "men", "cowok": "men", "laki": "men", "lelaki": "men", "men": "men", "man": "men",
    "wanita": "women", "cewek": "women", "perempuan": "women", "women": "women", "woman": "women",
}
_GENDER_RE = re.compile(r"(?:Gender|Target):\s*([A-Za-z]+)")
_CORE_SLOTS = ("upper", "lower", "feet")

# Order in which slots are swapped when a refresh must produce a new outfit.
_SWAP_ORDER = {"upper": 0, "outer": 1, "lower": 2, "feet": 3, "accessory": 4}


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


def _prompt_words(extra_prompt: str | None) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(extra_prompt or "")}


# Prompt words that name a specific slot. "baju" is used in everyday
# Indonesian for a top/shirt, so it maps to the top slot.
_SLOT_WORDS: dict[str, set[str]] = {
    "upper": {"baju", "atasan", "kemeja", "kaos", "kaus", "blus", "blouse", "shirt", "shirts",
              "tshirt", "tshirts", "top", "tops", "sweater", "hoodie", "sweatshirt", "kurta",
              "jaket", "jacket", "polo"},
    "lower": {"bawahan", "celana", "rok", "jeans", "pants", "trousers", "shorts", "legging",
              "leggings", "skirt"},
    "feet": {"sepatu", "sendal", "sandal", "sandals", "shoes", "sneakers", "heels", "alas"},
    "accessory:topi": {"topi", "cap", "hat"},
    "accessory:kacamata": {"kacamata", "glasses", "sunglasses"},
}
# Words that ask for a whole look rather than a single item.
_FULL_OUTFIT_WORDS = {"outfit", "setelan", "paket", "lengkap", "look", "penampilan", "pakaian"}
_ALL_ACCESSORY_WORDS = {"aksesori", "aksesoris", "accessory", "accessories"}
_SLOT_LABELS = {
    "upper": "1 atasan",
    "lower": "1 bawahan",
    "feet": "1 alas kaki (sepatu ATAU sendal, bukan keduanya)",
    "accessory:topi": "1 topi",
    "accessory:kacamata": "1 kacamata",
}


def requested_slots(extra_prompt: str | None) -> set[str]:
    """Which outfit slots the user asked for.

    "carikan baju formal" -> {"upper"}; "celana dan sepatu" -> {"lower", "feet"};
    no item named, or words like "outfit" -> top + bottom + footwear (plus any
    accessory that is mentioned).
    """
    words = _prompt_words(extra_prompt)
    slots = {s for s, ws in _SLOT_WORDS.items() if words & ws}
    if words & _ALL_ACCESSORY_WORDS:
        slots |= {"accessory:topi", "accessory:kacamata"}
    if not slots or words & _FULL_OUTFIT_WORDS:
        slots |= set(_CORE_SLOTS)
    return slots


def slot_group(item: Item) -> str:
    """slot_key, with jackets ('outer') counted as the top slot."""
    key = slot_key(item)
    return "upper" if key == "outer" else key


def _prompt_gender(extra_prompt: str | None) -> str | None:
    for w in _prompt_words(extra_prompt):
        if w in _GENDER_WORDS:
            return _GENDER_WORDS[w]
    return None


def _item_gender(item: Item) -> str | None:
    m = _GENDER_RE.search(item.description or "")
    return m.group(1).lower() if m else None


def _gender_ok(item: Item, gender: str | None) -> bool:
    g = _item_gender(item)
    return gender is None or g is None or g == "unisex" or g == gender


_KID_WORDS = {"anak", "kids", "kid", "boys", "girls", "balita"}


def _is_kids(item: Item) -> bool:
    text = f"{item.name} {_item_gender(item) or ''}".lower()
    return any(w in text for w in ("kids", "boys", "girls", "boy's", "girl's", "teen"))


def _fit_score(item: Item, terms: set[str], gender: str | None, allow_kids: bool) -> tuple:
    """Higher is better. Same gender and adult sizing (unless kids were asked
    for) come first, then how well the item matches the prompt."""
    return (
        _gender_ok(item, gender),
        allow_kids or not _is_kids(item),
        _relevance(item, terms),
    )


def _norm(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _relevance(item: Item, terms: set[str]) -> int:
    """Count prompt terms found as whole words in the item text, so 'shirt'
    does not match 'Tshirts'. Multi-word terms ('formal shoes') must appear
    as consecutive words."""
    if not terms:
        return 0
    raw = f"{item.name} {item.description or ''}".lower()
    raw = re.sub(r"\bt[\s-]?shirt", "tshirt", raw)  # a T-shirt is not a (formal) shirt
    words = [_norm(w) for w in _WORD_RE.findall(raw)]
    text = " " + " ".join(words) + " "
    score = 0
    for t in terms:
        phrase = " ".join(_norm(w) for w in t.lower().split())
        if f" {phrase} " in text:
            score += 1
    return score


def _spread_by_price(items: list[Item], n: int) -> list[Item]:
    """Pick n items spread evenly across the price range (cheap to pricey)."""
    if len(items) <= n:
        return items
    items = sorted(items, key=lambda it: float(it.price))
    step = (len(items) - 1) / (n - 1) if n > 1 else 0
    return [items[round(i * step)] for i in range(n)]


def _combo(items: Sequence[Item]) -> Combination:
    return frozenset(it.id for it in items)


class RecommendationService:
    def __init__(self, repo: ItemRepository):
        self.repo = repo
        self.gemini = get_gemini_client()

    # ------------------------------------------------ stage 1: shortlist
    @staticmethod
    def shortlist_candidates(
        catalog: Sequence[Item],
        extra_prompt: str | None,
        per_category: int,
        rng: random.Random | None = None,
    ) -> list[Item]:
        """Keep the most relevant items per category for the AI prompt.

        Items are ranked by keyword relevance to the user's prompt. Within the
        same relevance the shortlist is spread across the price range; on a
        refresh (`rng` given) it is sampled randomly instead, so a new request
        shows the AI different products.
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
                tier = tiers[score]
                if rng is not None:
                    picked.extend(rng.sample(tier, min(room, len(tier))))
                else:
                    picked.extend(_spread_by_price(tier, room))
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
        excluded: Sequence[Combination] = (),
        refresh: bool = False,
        slots: set[str] | None = None,
    ) -> dict:
        """Single Gemini call: analyse photo (if any), select items, explain.

        `candidates` must already be limited to the requested `slots`.
        """
        slots = slots or set(_CORE_SLOTS)
        order = ["upper", "lower", "feet", "accessory:topi", "accessory:kacamata"]
        wanted = ", ".join(_SLOT_LABELS[s] for s in order if s in slots)
        photo_rule = (
            '- Analisis foto pengguna terlampir (tone kulit, penampilan) dan isi "photo_analysis" 1 kalimat.\n'
            if user_image else
            '- Tidak ada foto; isi "photo_analysis" dengan string kosong.\n'
        )
        refresh_rule = ""
        if refresh:
            refresh_rule = "- Pengguna meminta ALTERNATIF: berikan kombinasi yang berbeda dari sebelumnya.\n"
        if excluded:
            combos = "; ".join("[" + ", ".join(str(i) for i in sorted(c)) + "]" for c in excluded)
            refresh_rule += (
                f"- JANGAN ulangi kombinasi id berikut persis sama: {combos}. "
                "Boleh memakai sebagian item yang sama asalkan kombinasinya berbeda.\n"
            )
        prompt = f"""Kamu fashion stylist. Pilih produk dari KATALOG sesuai permintaan pengguna.

ATURAN:
- Hanya pakai "id" yang ada di KATALOG. Jangan mengarang produk atau harga.
- Total harga TIDAK BOLEH melebihi budget {int(budget)}.
- Pilih TEPAT: {wanted}. Jangan memilih jenis produk lain.
- Semua item harus cocok untuk acara/gaya yang diminta (mis. acara formal: kemeja, celana formal, sepatu formal; bukan kaos olahraga, sandal jepit, atau legging).
- Gunakan gender yang konsisten untuk seluruh item (lihat "Gender" pada deskripsi); hindari produk anak kecuali diminta.
- Warna dapat disimpulkan dari nama/deskripsi.
- Jangan menyebut harga atau total harga di "reason" maupun "overall_reasoning" (total dihitung sistem).
{photo_rule}{refresh_rule}
PREFERENSI PENGGUNA: {extra_prompt or "(tidak ada)"}

KATALOG:
{self._catalog_as_text(candidates)}

Balas HANYA JSON:
{{"photo_analysis": "...", "selected": [{{"id": <int>, "reason": "<alasan singkat>"}}], "overall_reasoning": "<2-3 kalimat>"}}"""
        temperature = 0.9 if refresh else 0.4
        if user_image:
            return self.gemini.generate_json_with_image(
                prompt=prompt, image=user_image, image_mime=user_image_mime,
                max_output_tokens=1024, temperature=temperature,
            )
        return self.gemini.generate_json(prompt, max_output_tokens=1024, temperature=temperature)

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
            slot = slot_key(item)
            if slot in used_slots:
                continue  # one item per outfit slot
            used_slots.add(slot)
            chosen.append(item_id)
            reasons[item_id] = str(entry.get("reason", "")).strip() or "Dipilih oleh AI."
        return chosen, reasons

    @staticmethod
    def _fallback_selection(
        catalog: Sequence[Item],
        budget: float,
        extra_prompt: str | None = None,
        rng: random.Random | None = None,
    ) -> tuple[list[int], dict[int, str]]:
        """Rule-based pick used when the AI is unavailable (no AI call).

        Fills top, bottom, and footwear first; hats/glasses only when the
        prompt asks for them. In each slot it picks the item that best matches
        the prompt keywords and keeps one gender across the outfit, while
        reserving enough budget for the remaining core slots. Ties go to the
        cheapest item, or a random one on refresh.
        """
        terms = _query_terms(extra_prompt)
        gender = _prompt_gender(extra_prompt)
        allow_kids = bool(_prompt_words(extra_prompt) & _KID_WORDS)
        by_slot: dict[str, list[Item]] = defaultdict(list)
        for it in catalog:
            by_slot[slot_key(it)].append(it)

        # Slots whose best item matches the prompt most strongly get budget
        # first (e.g. formal shoes before a plain T-shirt), so a random or
        # pricey pick in a weak slot cannot crowd out the important one.
        def strength(slot: str) -> int:
            return max(_relevance(i, terms) for i in by_slot[slot])

        # `catalog` is already limited to the requested slots.
        slots = sorted((s for s in _CORE_SLOTS if by_slot.get(s)), key=strength, reverse=True)
        slots += sorted(s for s in by_slot if s.startswith("accessory:"))

        def good(i: Item) -> bool:
            return _gender_ok(i, gender) and (allow_kids or not _is_kids(i))

        def cheapest(slot: str) -> float:
            # Reserve money for a *suitable* item in later slots, not just
            # the cheapest one of any gender/size.
            pool = [i for i in by_slot[slot] if good(i)] or by_slot[slot]
            return min(float(i.price) for i in pool)

        chosen: list[int] = []
        reasons: dict[int, str] = {}
        remaining = budget
        for idx, slot in enumerate(slots):
            later_core = [s for s in slots[idx + 1:] if s in _CORE_SLOTS]
            reserve = sum(cheapest(s) for s in later_core)
            fits = [i for i in by_slot[slot] if float(i.price) + reserve <= remaining]
            if not fits:
                continue
            best = max(_fit_score(i, terms, gender, allow_kids) for i in fits)
            top = [i for i in fits if _fit_score(i, terms, gender, allow_kids) == best]
            pick = rng.choice(top) if rng else min(top, key=lambda i: float(i.price))
            gender = gender or (_item_gender(pick) if _item_gender(pick) != "unisex" else None)
            chosen.append(pick.id)
            reasons[pick.id] = (
                "Dipilih otomatis (layanan AI sedang tidak tersedia) berdasarkan kecocokan "
                "dengan prompt dan budget."
            )
            remaining -= float(pick.price)
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
    def complete_outfit(
        items: list[Item],
        preferred: Sequence[Item],
        catalog: Sequence[Item],
        budget: float,
        rng: random.Random | None,
        rank=None,
    ) -> tuple[list[Item], set[int]]:
        """Fill requested slots that are still empty, within budget.

        `catalog` is already limited to the requested slots, so only those are
        filled (asking for "baju" never adds pants). Used when the AI omits a
        slot or its pick was rejected. Shortlist candidates are tried before
        the rest of the catalog; on a refresh the choice is random among the
        best-ranked items that fit. Returns (items, ids_that_were_added).
        """
        groups = sorted({slot_group(i) for i in catalog},
                        key=lambda g: _SWAP_ORDER.get(g.split(":")[0], 9))
        core = [(g,) for g in groups]
        added: set[int] = set()
        result = list(items)

        def zone_of(i: Item) -> str:
            return slot_group(i)

        def best(cands: list[Item]) -> Item:
            if rank is not None:
                top = max(rank(i) for i in cands)
                cands = [i for i in cands if rank(i) == top]
            return rng.choice(cands) if rng else min(cands, key=lambda i: float(i.price))

        for zones in core:
            if any(zone_of(i) in zones for i in result):
                continue
            remaining = budget - sum(float(i.price) for i in result)
            taken = {r.id for r in result}
            slot_items = [i for i in catalog if zone_of(i) in zones and i.id not in taken]
            if not slot_items:
                continue
            fits = [i for i in preferred if zone_of(i) in zones and i.id not in taken
                    and float(i.price) <= remaining]
            fits = fits or [i for i in slot_items if float(i.price) <= remaining]
            if fits:
                pick = best(fits)
                result.append(pick)
                added.add(pick.id)
                continue

            # Not enough budget left: downgrade one chosen item to a cheaper one
            # in the same slot so the missing slot fits.
            cheapest = min(slot_items, key=lambda i: float(i.price))
            need = float(cheapest.price) - remaining
            swaps = []
            for k, cur in enumerate(result):
                alts = [a for a in catalog if slot_key(a) == slot_key(cur) and a.id not in taken
                        and float(a.price) <= float(cur.price) - need]
                if alts:
                    swaps.append((k, best(alts)))
            if not swaps:
                continue  # the budget really cannot cover a full outfit
            k, alt = max(swaps, key=lambda s: rank(s[1]) if rank else -float(s[1].price))
            result[k] = alt
            added.add(alt.id)
            remaining = budget - sum(float(i.price) for i in result)
            fill = [i for i in slot_items if float(i.price) <= remaining and i.id != alt.id]
            if fill:
                pick = best(fill)
                result.append(pick)
                added.add(pick.id)
        return result, added

    # --------------------------------- stage 4: guarantee a new combination
    @staticmethod
    def ensure_new_combination(
        items: list[Item],
        catalog: Sequence[Item],
        budget: float,
        excluded: set[Combination],
        rng: random.Random,
        rank=None,
    ) -> tuple[list[Item], set[int]]:
        """Return an outfit whose item set is not in `excluded`.

        If the picked outfit was already shown, swap items one slot at a time
        (tops first, then bottoms, footwear, accessories) with another catalog
        item of the same slot that keeps the total within budget. Items that
        are kept may repeat earlier outfits; only the full combination must be
        new. Returns (items, ids_that_were_swapped_in).
        """
        if _combo(items) not in excluded:
            return items, set()

        by_slot: dict[str, list[Item]] = defaultdict(list)
        for it in catalog:
            by_slot[slot_key(it)].append(it)

        def zone(it: Item) -> str:
            return it.category.body_zone if it.category else ""

        total = sum(float(i.price) for i in items)

        # 1) Swap one slot. All valid single swaps across every slot are
        #    compared, so a well-fitting new item in any slot beats a poor one
        #    (e.g. swap the shoes rather than force a kids' top). Ties prefer
        #    tops, then bottoms, footwear, accessories; then random.
        options = []
        for k, current in enumerate(items):
            for alt in by_slot[slot_key(current)]:
                if alt.id == current.id:
                    continue
                if total - float(current.price) + float(alt.price) > budget:
                    continue
                new_items = items[:k] + [alt] + items[k + 1:]
                if _combo(new_items) in excluded:
                    continue
                score = rank(alt) if rank is not None else ()
                options.append((score, -_SWAP_ORDER.get(zone(current), 9), rng.random(), new_items, alt))
        if options:
            *_, new_items, alt = max(options, key=lambda o: o[:3])
            return new_items, {alt.id}

        # 2) Add a requested slot that is not in the outfit yet (`catalog` only
        #    contains requested slots).
        used = {slot_key(i) for i in items}
        extras = [a for s, lst in by_slot.items() if s not in used for a in lst]
        rng.shuffle(extras)
        if rank is not None:
            extras.sort(key=rank, reverse=True)
        for alt in extras:
            if total + float(alt.price) <= budget and _combo(items + [alt]) not in excluded:
                return items + [alt], {alt.id}

        return items, set()

    @staticmethod
    def _interpret_previous(
        combos: Sequence[Combination], slots: set[str]
    ) -> set[Combination]:
        """Turn the client's previous combinations into the set to avoid.

        When several product types are requested but every entry holds one
        ID, the client most likely split ONE previous outfit into one field
        per ID (common with array inputs in API GUIs), so they are merged.
        For a single product type, one-ID entries are separate previous picks.
        """
        if len(slots) > 1 and len(combos) > 1 and all(len(c) == 1 for c in combos):
            return {frozenset().union(*combos)}
        return set(combos)

    @staticmethod
    def _default_reasoning(items: Sequence[Item], extra_prompt: str | None) -> str:
        names = ", ".join(it.name for it in items)
        style = f" untuk kebutuhan '{extra_prompt}'" if extra_prompt else ""
        what = "Kombinasi" if len(items) > 1 else "Produk"
        return f"{what} {names} dipilih{style} dan tetap sesuai budget Anda."

    # ------------------------------------------------------- orchestrate
    def recommend(
        self,
        *,
        user_image: bytes | None,
        user_image_mime: str,
        budget: float,
        extra_prompt: str | None,
        mode: str = "mannequin",
        refresh: bool = False,
        previous_combinations: Sequence[Combination] = (),
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
                "combination": [], "notice": None,
            }

        # Only the slots the user asked for take part from here on: the AI
        # never sees other products, and the server never adds them.
        slots = requested_slots(extra_prompt)
        catalog = [i for i in catalog if slot_group(i) in slots]
        if not catalog:
            return {
                "chosen_items": [], "reasons": {}, "total_price": 0.0,
                "photo_analysis": "",
                "overall_reasoning": (
                    "Tidak ada produk untuk jenis yang diminta dengan harga di bawah budget "
                    f"{budget}. Coba naikkan budget atau ubah prompt."
                ),
                "generated_image_path": None, "image_editing_error": None,
                "combination": [], "notice": None,
            }

        excluded = self._interpret_previous(previous_combinations, slots)
        rng = random.Random() if refresh else None
        candidates = self.shortlist_candidates(
            catalog, extra_prompt, settings.reco_max_per_category, rng=rng
        )
        valid = {it.id: it for it in candidates}

        photo_analysis, overall = "", ""
        try:
            result = self.ask_ai(
                candidates=candidates, budget=budget, extra_prompt=extra_prompt,
                user_image=user_image, user_image_mime=user_image_mime,
                excluded=list(excluded), refresh=refresh, slots=slots,
            )
            chosen_ids, reasons = self._parse_selection(result, valid)
            photo_analysis = str(result.get("photo_analysis", "") or "").strip()
            overall = str(result.get("overall_reasoning", "") or "").strip()
        except Exception as exc:
            logger.warning("Gemini gagal, pakai fallback budget: %s", exc)
            chosen_ids, reasons = [], {}

        if not chosen_ids:
            chosen_ids, reasons = self._fallback_selection(catalog, budget, extra_prompt, rng)
            valid = {it.id: it for it in catalog}
            overall = ""

        # Prices come from the DB records, never from the AI output.
        chosen_items = self.enforce_budget([valid[i] for i in chosen_ids], budget)

        # Ranking used when the server has to pick items itself (completing
        # the outfit or forcing a new combination).
        terms = _query_terms(extra_prompt)
        allow_kids = bool(_prompt_words(extra_prompt) & _KID_WORDS)
        gender = _prompt_gender(extra_prompt) or next(
            (g for g in map(_item_gender, chosen_items) if g and g != "unisex"), None
        )

        def rank(i: Item) -> tuple:
            return _fit_score(i, terms, gender, allow_kids)

        before = set(chosen_ids)  # the AI's own picks, before any server change
        chosen_items, filled = self.complete_outfit(
            chosen_items, candidates, catalog, budget, rng, rank=rank
        )
        for item_id in filled:
            reasons[item_id] = "Ditambahkan agar semua jenis produk yang diminta terpenuhi."
        if filled and overall:
            if before <= {i.id for i in chosen_items}:
                # AI picks kept: just mention what the server added.
                names = ", ".join(i.name for i in chosen_items if i.id in filled)
                overall = f"{overall} Sistem menambahkan {names} agar permintaan lengkap dan tetap sesuai budget."
            else:
                overall = ""  # an AI pick was replaced; its text no longer matches

        notice = None
        if excluded:
            chosen_items, swapped = self.ensure_new_combination(
                chosen_items, catalog, budget, excluded, rng or random.Random(), rank=rank,
            )
            if swapped:
                overall = ""  # the AI text describes the outfit before the swap
                for item_id in swapped:
                    reasons[item_id] = (
                        "Dipilih sebagai alternatif agar kombinasi berbeda dari opsi sebelumnya."
                    )
            elif _combo(chosen_items) in excluded:
                notice = (
                    "Tidak ditemukan kombinasi baru yang sesuai budget; kombinasi ini "
                    "sama dengan opsi sebelumnya. Coba naikkan budget atau ubah prompt."
                )

        total_price = round(sum(float(it.price) for it in chosen_items), 2)

        generated_path, image_error = None, None
        if chosen_items:
            generated_path, image_error = outfit_service.build_visual(
                chosen_items, mode, user_image
            )

        return {
            "chosen_items": chosen_items,
            "reasons": reasons,
            "total_price": total_price,
            "photo_analysis": photo_analysis,
            "overall_reasoning": overall or self._default_reasoning(chosen_items, extra_prompt),
            "generated_image_path": generated_path,
            "image_editing_error": image_error,
            "combination": sorted(it.id for it in chosen_items),
            "notice": notice,
        }
