"""Local, free outfit visualization using Pillow.

Builds an outfit preview by compositing product images onto either a drawn
mannequin silhouette or the user's own photo. No paid AI image generation.

Product photos usually have a white/plain background. Before compositing we
remove that background (via `rembg` if available, otherwise a lightweight
white-threshold fallback) and auto-crop transparent margins, so garments sit
cleanly on the body instead of as white boxes.

Placement is category-driven through Category.body_zone:
  upper  -> torso (shirt/top)
  outer  -> over the torso (jacket/cardigan)
  lower  -> legs (pants/skirt)
  feet   -> shoes
  accessory -> head (hat/glasses) or side panel
"""
from __future__ import annotations

import io
import logging
import threading
from dataclasses import dataclass
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from app.services import storage_service

logger = logging.getLogger(__name__)

CANVAS_W = 720
CANVAS_H = 1080
BG_COLOR = (245, 246, 248)
MANNEQUIN_COLOR = (210, 215, 223)
MANNEQUIN_OUTLINE = (182, 188, 198)

SIDE_ZONE_X = 0.86          # x position (ratio) of the accessory side panel

CATEGORY_KEYWORDS = {
    "outer": ["luaran", "outer", "jacket", "jaket", "coat", "mantel", "cardigan"],
    "upper": ["atasan", "top", "shirt", "kemeja", "kaos", "blouse", "sweater", "tshirt", "t-shirt"],
    "lower": ["bawahan", "bottom", "celana", "pants", "rok", "skirt", "short", "jeans", "chino"],
    "feet": ["sepatu", "shoes", "footwear", "sneakers", "boots", "sandal", "loafer", "sendal"],
    "head": ["topi", "hat", "cap", "kacamata", "glasses", "sunglasses"],
}


@dataclass
class OutfitPiece:
    name: str
    body_zone: str          # from Category.body_zone: upper/lower/feet/outer/accessory
    category_name: str      # human-readable category name (fallback classification)
    image_rel_path: str


_BODY_ZONE_MAP = {
    "upper": "upper",
    "lower": "lower",
    "feet": "feet",
    "outer": "outer",
    "accessory": "head",
}

# Lazily-created rembg session (loaded once, shared across request threads).
_rembg_session = None
_rembg_failed = False
_rembg_lock = threading.Lock()

# Cutout results cached in memory: product images never change (unique file
# names), so background removal only runs once per product per process.
_CUTOUT_CACHE_SIZE = 512


def _classify(piece: "OutfitPiece") -> str:
    zone = (piece.body_zone or "").lower()
    if zone in _BODY_ZONE_MAP:
        internal = _BODY_ZONE_MAP[zone]
        # Accessory: glasses/hat go on head, others to the side panel.
        return internal
    cat = (piece.category_name or "").lower()
    for z, keywords in CATEGORY_KEYWORDS.items():
        if any(k in cat for k in keywords):
            return z if z != "head" else "head"
    return "side"


# ---------------------------------------------------------------- bg removal
def _get_rembg_session():
    global _rembg_session
    if _rembg_session is None:
        with _rembg_lock:
            if _rembg_session is None:
                from rembg import new_session

                _rembg_session = new_session("u2netp")  # lighter model
    return _rembg_session


def warm_up() -> None:
    """Load the rembg model ahead of time (called at app startup)."""
    try:
        _get_rembg_session()
        logger.info("rembg siap.")
    except Exception as exc:
        logger.warning("Warm-up rembg gagal: %s", exc)


def _remove_bg_rembg(img: Image.Image) -> Image.Image | None:
    global _rembg_failed
    if _rembg_failed:
        return None
    try:
        from rembg import remove

        out = remove(img, session=_get_rembg_session())
        return out.convert("RGBA")
    except Exception as exc:
        logger.warning("rembg tidak tersedia, pakai fallback: %s", exc)
        _rembg_failed = True
        return None


def _remove_bg_threshold(img: Image.Image, thresh: int = 238) -> Image.Image:
    """Fallback: make near-white pixels transparent."""
    img = img.convert("RGBA")
    datas = img.getdata()
    new = []
    for r, g, b, a in datas:
        if r >= thresh and g >= thresh and b >= thresh:
            new.append((r, g, b, 0))
        else:
            new.append((r, g, b, a))
    img.putdata(new)
    return img


def _keep_largest_component(img: Image.Image, min_ratio: float = 1.0) -> Image.Image:
    """Keep the largest opaque blob, dropping stray marks (e.g. watermarks).

    Works on the alpha channel: label connected non-transparent regions and
    keep the biggest one, plus any other region at least `min_ratio` of its
    size (so both shoes of a pair survive while small text is dropped).
    Falls back to the original image on any error.
    """
    try:
        import numpy as np
        from scipy import ndimage

        arr = np.array(img)
        alpha = arr[:, :, 3]
        mask = alpha > 25
        if not mask.any():
            return img
        labeled, n = ndimage.label(mask)
        if n <= 1:
            return img
        # Find the largest labeled component.
        sizes = ndimage.sum(mask, labeled, range(1, n + 1))
        big = [k + 1 for k, s in enumerate(sizes) if s >= sizes.max() * min_ratio]
        keep = np.isin(labeled, big)
        arr[:, :, 3] = np.where(keep, alpha, 0)
        return Image.fromarray(arr, "RGBA")
    except Exception as exc:  # scipy/numpy missing or any failure
        logger.warning("keep_largest_component dilewati: %s", exc)
        return img


def _clean_edges(img: Image.Image) -> Image.Image:
    """Remove the faint grey halo/shadow left around a cutout.

    Drops weak alpha (soft shadows) and shrinks the mask by 1px so the edge
    sits cleanly on the garment.
    """
    from PIL import ImageFilter

    r, g, b, a = img.split()
    a = a.point(lambda v: 0 if v < 110 else v)
    a = a.filter(ImageFilter.MinFilter(3))
    return Image.merge("RGBA", (r, g, b, a))


def _cutout(img: Image.Image) -> Image.Image:
    """Remove background, drop stray marks, then auto-crop transparent margins."""
    result = _remove_bg_rembg(img)
    if result is None:
        result = _remove_bg_threshold(img)
    result = _clean_edges(result)
    # Keep big parts too: a pair of shoes is two separate blobs.
    result = _keep_largest_component(result, min_ratio=0.25)
    bbox = result.getbbox()
    if bbox:
        result = result.crop(bbox)
    return result


@lru_cache(maxsize=_CUTOUT_CACHE_SIZE)
def _load_cutout(rel_path: str) -> Image.Image | None:
    """Background-removed product image (cached; callers must not mutate it)."""
    try:
        data = storage_service.read_bytes(rel_path)
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        return _cutout(img)
    except Exception as exc:
        logger.warning("Gagal memuat/cutout gambar %s: %s", rel_path, exc)
        return None


def _fit(img: Image.Image, max_w: int, max_h: int) -> Image.Image:
    img = img.copy()
    img.thumbnail((max_w, max_h), Image.LANCZOS)
    return img


def _fit_to_width(img: Image.Image, target_w: int, max_h: int) -> Image.Image:
    """Scale to a target width (keeping ratio), but never exceed max_h."""
    img = img.copy()
    scale = target_w / img.width
    h = int(img.height * scale)
    if h > max_h:
        scale = max_h / img.height
    new_w = max(1, int(img.width * scale))
    new_h = max(1, int(img.height * scale))
    return img.resize((new_w, new_h), Image.LANCZOS)


def _fit_cover_width(img: Image.Image, target_w: int) -> Image.Image:
    """Scale to exactly target_w (keeping ratio), ignoring height.

    Guarantees the garment is at least target_w wide so it always covers the
    mannequin body underneath (no shoulders peeking out).
    """
    img = img.copy()
    scale = target_w / img.width
    new_w = max(1, int(img.width * scale))
    new_h = max(1, int(img.height * scale))
    return img.resize((new_w, new_h), Image.LANCZOS)


def _paste_center(canvas: Image.Image, img: Image.Image, cx: int, cy: int) -> None:
    x = int(cx - img.width / 2)
    y = int(cy - img.height / 2)
    canvas.paste(img, (x, y), img)


def _paste_anchored(
    canvas: Image.Image, img: Image.Image, cx: int, y_anchor: int, anchor: str
) -> None:
    """Paste horizontally centered at cx, vertically anchored.

    anchor: 'top'    -> img top at y_anchor
            'bottom' -> img bottom at y_anchor
            'center' -> img center at y_anchor
    """
    x = int(cx - img.width / 2)
    if anchor == "top":
        y = y_anchor
    elif anchor == "bottom":
        y = y_anchor - img.height
    else:
        y = int(y_anchor - img.height / 2)
    canvas.paste(img, (x, y), img)


def _detect_person_bbox(photo_rgba: Image.Image) -> tuple[int, int, int, int] | None:
    """Return (left, top, right, bottom) of the person in the photo, or None."""
    cut = _remove_bg_rembg(photo_rgba.copy())
    if cut is None:
        return None
    cut = _keep_largest_component(cut)
    return cut.getbbox()


def _base_canvas_user(user_image: bytes) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Place the user's photo on the canvas and return the person box in canvas
    coordinates (left, top, right, bottom)."""
    area_h = CANVAS_H - CAPTION_H
    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), BG_COLOR + (255,))
    photo = Image.open(io.BytesIO(user_image)).convert("RGBA")
    photo = _fit(photo, CANVAS_W, area_h)
    px = (CANVAS_W - photo.width) // 2
    py = (area_h - photo.height) // 2
    canvas.paste(photo, (px, py), photo)

    bbox = _detect_person_bbox(photo)
    if bbox:
        box = (bbox[0] + px, bbox[1] + py, bbox[2] + px, bbox[3] + py)
    else:
        # Person not detected: assume a centred person filling the photo height.
        box = (px + int(photo.width * 0.3), py, px + int(photo.width * 0.7), py + photo.height)
    return canvas, box


def _is_shorts(piece: OutfitPiece) -> bool:
    words = set(f"{piece.category_name} {piece.name}".lower().split())
    return any(w in words for w in SHORTS_WORDS)


def _is_glasses(piece: OutfitPiece) -> bool:
    name = f"{piece.category_name} {piece.name}".lower()
    return any(k in name for k in ("kacamata", "glasses", "sunglasses"))


# ------------------------------------------------------------ mannequin mode
# Proportions relative to the reference top width W (unscaled figure space).
REF_W = 300                 # reference width of the top garment
HEAD_R_RATIO = 0.155        # head radius = W * ratio
NECK_H_RATIO = 0.035        # visible neck height = W * ratio
PANTS_W_RATIO = 0.68        # pants width vs top width (hips < shoulders)
TUCK_RATIO = 0.07           # pants waist overlaps top hem by this * top height
PANTS_MAX_H_RATIO = 1.38    # pants height cap vs top height (natural leg length)
PANTS_MIN_H_RATIO = 1.15    # pants height floor vs top height
FIG_MARGIN_TOP = 40
FIG_MARGIN_BOTTOM = 30      # gap kept above the caption bar
CAPTION_H = 46


SHOE_EACH_W_RATIO = 0.30    # width of ONE shoe vs top width
SHOE_SQUASH = 0.62          # vertical squash: top-down product shots read as feet seen from the front
SHOE_MAX_H_RATIO = 0.26     # height cap of one shoe vs top width
SHOE_TUCK = 0.15            # share of the shoe height hidden under the pants hem
SHORTS_H_RATIO = 0.62       # shorts height vs top height (legs show below)
SHORTS_WORDS = ("pendek", "short", "shorts")


def _split_shoes(img: Image.Image) -> tuple[Image.Image, Image.Image]:
    """Return (left, right) shoe images.

    Product photos show either a pair (two blobs) or a single shoe. A single
    shoe is mirrored to make the left foot.
    """
    parts: list[tuple[int, Image.Image]] = []
    try:
        import numpy as np
        from scipy import ndimage

        alpha = np.array(img.getchannel("A")) > 25
        labeled, n = ndimage.label(alpha)
        if n >= 2:
            sizes = ndimage.sum(alpha, labeled, range(1, n + 1))
            order = sorted(range(n), key=lambda k: sizes[k], reverse=True)[:2]
            if sizes[order[1]] >= sizes[order[0]] * 0.25:
                for k in order:
                    sl = ndimage.find_objects((labeled == k + 1).astype(int))[0]
                    y0, y1, x0, x1 = sl[0].start, sl[0].stop, sl[1].start, sl[1].stop
                    piece = img.crop((x0, y0, x1, y1))
                    mask = Image.fromarray(((labeled[y0:y1, x0:x1] == k + 1) * 255).astype("uint8"))
                    piece.putalpha(Image.fromarray(np.minimum(
                        np.array(piece.getchannel("A")), np.array(mask))))
                    parts.append((x0, piece))
    except Exception as exc:
        logger.warning("Pemisahan sepatu dilewati: %s", exc)
    if len(parts) == 2:
        parts.sort(key=lambda p: p[0])
        return parts[0][1], parts[1][1]

    # Pairs that touch form one blob: split at the emptiest column near the middle.
    try:
        import numpy as np

        cols = (np.array(img.getchannel("A")) > 25).sum(axis=0)
        lo, hi = int(len(cols) * 0.3), int(len(cols) * 0.7)
        if hi > lo and cols.max() > 0:
            cut = lo + int(np.argmin(cols[lo:hi]))
            if cols[cut] <= cols.max() * 0.2:
                left, right = img.crop((0, 0, cut, img.height)), img.crop((cut, 0, img.width, img.height))
                left, right = left.crop(left.getbbox()), right.crop(right.getbbox())
                return left, right
    except Exception as exc:
        logger.warning("Pemisahan sepatu dilewati: %s", exc)
    from PIL import ImageOps

    return ImageOps.mirror(img), img


def _shoe_pair(img: Image.Image, each_w: int, max_h: int) -> tuple[Image.Image, Image.Image]:
    """Scale both shoes to the same size, foreshortened like feet seen from the front."""
    left, right = _split_shoes(img)
    out = []
    for shoe in (left, right):
        w = each_w
        h = int(shoe.height * (each_w / shoe.width) * SHOE_SQUASH)
        if h > max_h:
            w, h = int(w * max_h / h), max_h
        out.append(shoe.resize((max(1, w), max(1, h)), Image.LANCZOS))
    # Same height for both feet so the pair looks level.
    h = min(o.height for o in out)
    out = [o.resize((max(1, int(o.width * h / o.height)), h), Image.LANCZOS) for o in out]
    return out[0], out[1]


def _paste_shoes(canvas: Image.Image, pair: tuple[Image.Image, Image.Image],
                 cx: int, gap: int, top_y: int) -> None:
    """Paste the pair centred on the two legs (leg centres at cx ± gap)."""
    left, right = pair
    canvas.paste(left, (cx - gap - left.width // 2, top_y), left)
    canvas.paste(right, (cx + gap - right.width // 2, top_y), right)


def _scale_to_height(img: Image.Image, target_h: int) -> Image.Image:
    s = target_h / img.height
    return img.resize((max(1, int(img.width * s)), max(1, target_h)), Image.LANCZOS)


def _compose_on_mannequin(pieces: list[OutfitPiece]) -> Image.Image:
    """Stack garments head-to-toe, draw a mannequin fitted behind them, then
    scale the whole figure to fit the frame (nothing can be cut off)."""
    W = REF_W
    shorts = False
    tops: dict[str, Image.Image] = {}
    head_items: list[tuple[OutfitPiece, Image.Image]] = []
    side_items: list[Image.Image] = []
    for piece in pieces:
        zone = _classify(piece)
        img = _load_cutout(piece.image_rel_path)
        if img is None:
            continue
        if zone in ("upper", "outer", "lower", "feet"):
            if zone not in tops:
                tops[zone] = img
                if zone == "lower":
                    shorts = _is_shorts(piece)
        elif zone == "head":
            head_items.append((piece, img))
        else:
            side_items.append(img)

    top = _fit_cover_width(tops["upper"], W) if "upper" in tops else None
    outer = _fit_cover_width(tops["outer"], int(W * 1.08)) if "outer" in tops else None
    main_top = outer or top
    top_h = main_top.height if main_top else int(W * 1.05)

    pants = None
    if "lower" in tops:
        pants = _fit_cover_width(tops["lower"], int(W * PANTS_W_RATIO))
        # Keep a natural torso:leg ratio regardless of the product photo.
        max_h = int(top_h * PANTS_MAX_H_RATIO)
        min_h = int(top_h * PANTS_MIN_H_RATIO)
        if shorts:
            # Shorts end above the knee; the bare legs are drawn below them.
            if pants.height > int(top_h * SHORTS_H_RATIO):
                pants = _scale_to_height(pants, int(top_h * SHORTS_H_RATIO))
        elif pants.height > max_h:
            pants = _scale_to_height(pants, max_h)
        elif pants.height < min_h:
            pants = _scale_to_height(pants, min_h)
    # Leg length stays natural even when the bottom is short.
    pants_h = pants.height if pants and not shorts else int(top_h * 1.3)

    # Leg centres sit about a quarter of the pants width from the middle.
    leg_gap = int((pants.width if pants else W * 0.5) * 0.25)
    shoes = None
    if "feet" in tops:
        each_w = min(int(W * SHOE_EACH_W_RATIO), int(leg_gap * 1.9))
        shoes = _shoe_pair(tops["feet"], each_w, int(W * SHOE_MAX_H_RATIO))
    shoe_h = shoes[0].height if shoes else int(W * 0.18)

    head_r = int(W * HEAD_R_RATIO)
    neck_h = int(W * NECK_H_RATIO)

    # Vertical stacking in unscaled figure space (y grows downwards).
    head_top = int(head_r * 0.6) if head_items else 0      # room for a hat
    head_cy = head_top + head_r
    top_y = head_cy + head_r + neck_h                       # collar line
    top_bottom = top_y + top_h
    pants_y = top_bottom - int(top_h * TUCK_RATIO)
    pants_bottom = pants_y + pants_h
    shoe_y = pants_bottom - int(shoe_h * SHOE_TUCK)
    fig_h = shoe_y + shoe_h

    widths = [W, int(W * 1.1)]
    if outer:
        widths.append(outer.width)
    fig_w = max(widths) + 40
    fcx = fig_w // 2

    fig = Image.new("RGBA", (fig_w, fig_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(fig)

    # --- mannequin body (drawn first, stays hidden behind the clothes) -----
    neck_half = int(head_r * 0.58)
    d.rectangle([fcx - neck_half, head_cy, fcx + neck_half, top_y + int(top_h * 0.15)],
                fill=MANNEQUIN_COLOR)
    torso_half = int(W * 0.24)   # narrower than the shirt body -> never peeks out
    d.polygon([(fcx - torso_half, top_y + int(top_h * 0.10)),
               (fcx + torso_half, top_y + int(top_h * 0.10)),
               (fcx + int(torso_half * 0.9), top_bottom),
               (fcx - int(torso_half * 0.9), top_bottom)], fill=MANNEQUIN_COLOR)
    if pants is None or shorts:
        leg_half = int(W * 0.09)
        for sx in (-1, 1):
            lx = fcx + sx * leg_gap
            d.polygon([(lx - leg_half, pants_y), (lx + leg_half, pants_y),
                       (lx + int(leg_half * 0.7), shoe_y + 10),
                       (lx - int(leg_half * 0.7), shoe_y + 10)], fill=MANNEQUIN_COLOR)
    d.ellipse([fcx - head_r, head_cy - head_r, fcx + head_r, head_cy + head_r],
              fill=MANNEQUIN_COLOR, outline=MANNEQUIN_OUTLINE, width=3)

    # --- garments: shoes, pants, top, outer (so the top covers the pants waist)
    if shoes:
        _paste_shoes(fig, shoes, fcx, leg_gap, shoe_y)
    if pants:
        fig.paste(pants, (fcx - pants.width // 2, pants_y), pants)
    if top:
        fig.paste(top, (fcx - top.width // 2, top_y), top)
    if outer:
        fig.paste(outer, (fcx - outer.width // 2, top_y - 4), outer)

    # Head accessories: glasses at eye level, hats on top of the head.
    for piece, img in head_items:
        if _is_glasses(piece):
            g = _fit_cover_width(img, int(head_r * 2.1))
            fig.paste(g, (fcx - g.width // 2, head_cy - int(head_r * 0.15) - g.height // 2), g)
        else:
            h = _fit_cover_width(img, int(head_r * 2.5))
            fig.paste(h, (fcx - h.width // 2, head_cy - int(head_r * 0.35) - h.height), h)

    # --- scale the whole figure to fit the frame ---------------------------
    avail_h = CANVAS_H - CAPTION_H - FIG_MARGIN_TOP - FIG_MARGIN_BOTTOM
    avail_w = CANVAS_W - (220 if side_items else 60)
    scale = min(avail_h / fig.height, avail_w / fig.width)
    fig = fig.resize((max(1, int(fig.width * scale)), max(1, int(fig.height * scale))),
                     Image.LANCZOS)

    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), BG_COLOR + (255,))
    fig_x = (CANVAS_W - fig.width) // 2 - (70 if side_items else 0)
    fig_y = FIG_MARGIN_TOP + (avail_h - fig.height) // 2
    canvas.paste(fig, (fig_x, fig_y), fig)

    # Other accessories (not head-worn) stacked in a side panel.
    for i, img in enumerate(side_items):
        s = _fit(img, 150, 120)
        _paste_center(canvas, s, int(SIDE_ZONE_X * CANVAS_W), 200 + i * 150)
    return canvas


# ----------------------------------------------------------- user photo mode
# Landmarks as a fraction of the person's height (head top = 0, feet = 1).
PHOTO_COLLAR_Y = 0.13       # top of the shirt collar
PHOTO_TOP_HEM_Y = 0.53      # bottom hem of the top
PHOTO_WAIST_Y = 0.47        # pants waistband
PHOTO_ANKLE_Y = 0.95        # pants hem
PHOTO_HEAD_CY = 0.065       # head centre
PHOTO_HEAD_R = 0.065        # head radius


def _compose_on_user_photo(pieces: list[OutfitPiece], user_image: bytes) -> Image.Image:
    """Dress the person in the user's photo, sizing each garment from the
    person's detected height so proportions match the body."""
    canvas, (l, t, r, b) = _base_canvas_user(user_image)
    h = b - t
    cx = (l + r) // 2

    cuts: dict[str, Image.Image] = {}
    shorts = False
    head_items: list[tuple[OutfitPiece, Image.Image]] = []
    side_items: list[Image.Image] = []
    for piece in pieces:
        zone = _classify(piece)
        img = _load_cutout(piece.image_rel_path)
        if img is None:
            continue
        if zone in ("upper", "outer", "lower", "feet"):
            if zone not in cuts:
                cuts[zone] = img
                if zone == "lower":
                    shorts = _is_shorts(piece)
        elif zone == "head":
            head_items.append((piece, img))
        else:
            side_items.append(img)

    collar_y = t + int(PHOTO_COLLAR_Y * h)
    top_h = int((PHOTO_TOP_HEM_Y - PHOTO_COLLAR_Y) * h)
    top_img = cuts.get("outer") or cuts.get("upper")
    top = _scale_to_height(top_img, top_h) if top_img else None
    ref_w = top.width if top else int(h * 0.30)

    pants = None
    if "lower" in cuts:
        leg_len = (PHOTO_ANKLE_Y - PHOTO_WAIST_Y) * h
        pants = _scale_to_height(cuts["lower"], int(leg_len * (0.45 if shorts else 1.0)))
    if "feet" in cuts:
        leg_gap = int((pants.width if pants else ref_w * 0.5) * 0.25)
        each_w = min(int(ref_w * SHOE_EACH_W_RATIO), int(leg_gap * 1.9))
        pair = _shoe_pair(cuts["feet"], each_w, int(h * 0.07))
        _paste_shoes(canvas, pair, cx, leg_gap, b - pair[0].height)
    if pants:
        waist_y = t + int(PHOTO_WAIST_Y * h)
        canvas.paste(pants, (cx - pants.width // 2, waist_y), pants)
    if top:
        canvas.paste(top, (cx - top.width // 2, collar_y), top)

    head_cy = t + int(PHOTO_HEAD_CY * h)
    head_r = int(PHOTO_HEAD_R * h)
    for piece, img in head_items:
        if _is_glasses(piece):
            g = _fit_cover_width(img, int(head_r * 2.1))
            canvas.paste(g, (cx - g.width // 2, head_cy - int(head_r * 0.15) - g.height // 2), g)
        else:
            hat = _fit_cover_width(img, int(head_r * 2.5))
            canvas.paste(hat, (cx - hat.width // 2, head_cy - int(head_r * 0.35) - hat.height), hat)

    for i, img in enumerate(side_items):
        s = _fit(img, 150, 120)
        _paste_center(canvas, s, int(SIDE_ZONE_X * CANVAS_W), 200 + i * 150)
    return canvas


def build_outfit_image(
    *,
    pieces: list[OutfitPiece],
    mode: str = "mannequin",
    user_image: bytes | None = None,
) -> str:
    """Compose an outfit preview and return its public-relative path."""
    if mode == "user_photo" and user_image:
        canvas = _compose_on_user_photo(pieces, user_image)
    else:
        canvas = _compose_on_mannequin(pieces)

    # Caption
    d = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("arial.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    d.rectangle([0, CANVAS_H - CAPTION_H, CANVAS_W, CANVAS_H], fill=(255, 255, 255, 235))
    d.text((20, CANVAS_H - 38), "Pratinjau Outfit", fill=(60, 60, 60), font=font)

    out = canvas.convert("RGB")
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return storage_service.save_generated_image(buf.getvalue(), ext=".png")
