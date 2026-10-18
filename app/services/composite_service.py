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
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

from app.services import storage_service

logger = logging.getLogger(__name__)

CANVAS_W = 720
CANVAS_H = 1080
BG_COLOR = (245, 246, 248)
MANNEQUIN_COLOR = (208, 213, 221)
MANNEQUIN_OUTLINE = (176, 182, 192)

# Zones: (center_x_ratio, center_y_ratio, max_w, max_h).
# Sizes are generous so garments overlap the body for a "worn" look.
UPPER_ZONE = (0.5, 0.36, 300, 320)
OUTER_ZONE = (0.5, 0.36, 360, 360)
LOWER_ZONE = (0.5, 0.68, 300, 400)
FEET_ZONE = (0.5, 0.91, 210, 140)
HEAD_ZONE = (0.5, 0.10, 170, 150)
SIDE_ZONE_X = 0.86

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

# Lazily-created rembg session (loaded once).
_rembg_session = None
_rembg_failed = False


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
def _remove_bg_rembg(img: Image.Image) -> Image.Image | None:
    global _rembg_session, _rembg_failed
    if _rembg_failed:
        return None
    try:
        from rembg import remove, new_session

        if _rembg_session is None:
            _rembg_session = new_session("u2netp")  # lighter model
        out = remove(img, session=_rembg_session)
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


def _keep_largest_component(img: Image.Image) -> Image.Image:
    """Keep only the largest opaque blob, dropping stray marks (e.g. watermarks).

    Works on the alpha channel: label connected non-transparent regions and
    keep the biggest one. Falls back to the original image on any error.
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
        largest = int(np.argmax(sizes)) + 1
        keep = labeled == largest
        arr[:, :, 3] = np.where(keep, alpha, 0)
        return Image.fromarray(arr, "RGBA")
    except Exception as exc:  # scipy/numpy missing or any failure
        logger.warning("keep_largest_component dilewati: %s", exc)
        return img


def _cutout(img: Image.Image) -> Image.Image:
    """Remove background, drop stray marks, then auto-crop transparent margins."""
    result = _remove_bg_rembg(img)
    if result is None:
        result = _remove_bg_threshold(img)
    result = _keep_largest_component(result)
    bbox = result.getbbox()
    if bbox:
        result = result.crop(bbox)
    return result


def _load_cutout(rel_path: str) -> Image.Image | None:
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


def _draw_mannequin(canvas: Image.Image) -> None:
    d = ImageDraw.Draw(canvas)
    cx = CANVAS_W // 2
    d.ellipse([cx - 52, 60, cx + 52, 165], fill=MANNEQUIN_COLOR, outline=MANNEQUIN_OUTLINE, width=3)
    d.rectangle([cx - 20, 160, cx + 20, 205], fill=MANNEQUIN_COLOR)
    d.polygon(
        [(cx - 115, 210), (cx + 115, 210), (cx + 92, 580), (cx - 92, 580)],
        fill=MANNEQUIN_COLOR, outline=MANNEQUIN_OUTLINE,
    )
    d.polygon([(cx - 115, 215), (cx - 150, 500), (cx - 116, 505), (cx - 90, 250)], fill=MANNEQUIN_COLOR)
    d.polygon([(cx + 115, 215), (cx + 150, 500), (cx + 116, 505), (cx + 90, 250)], fill=MANNEQUIN_COLOR)
    d.polygon([(cx - 92, 580), (cx - 16, 580), (cx - 26, 980), (cx - 82, 980)], fill=MANNEQUIN_COLOR)
    d.polygon([(cx + 92, 580), (cx + 16, 580), (cx + 26, 980), (cx + 82, 980)], fill=MANNEQUIN_COLOR)


def _zone_point(zone: tuple[float, float, int, int]) -> tuple[int, int, int, int]:
    rx, ry, mw, mh = zone
    return int(rx * CANVAS_W), int(ry * CANVAS_H), mw, mh


def _base_canvas_mannequin() -> Image.Image:
    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), BG_COLOR + (255,))
    _draw_mannequin(canvas)
    return canvas


def _base_canvas_user(user_image: bytes) -> Image.Image:
    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), BG_COLOR + (255,))
    try:
        photo = Image.open(io.BytesIO(user_image)).convert("RGBA")
        photo = _fit(photo, CANVAS_W, CANVAS_H)
        _paste_center(canvas, photo, CANVAS_W // 2, CANVAS_H // 2)
    except Exception:
        _draw_mannequin(canvas)
    return canvas


def build_outfit_image(
    *,
    pieces: list[OutfitPiece],
    mode: str = "mannequin",
    user_image: bytes | None = None,
) -> str:
    """Compose an outfit preview and return its public-relative path."""
    if mode == "user_photo" and user_image:
        canvas = _base_canvas_user(user_image)
    else:
        canvas = _base_canvas_mannequin()

    cx = CANVAS_W // 2

    # Fixed body anchor lines (y px on the mannequin). Garments overlap here so
    # there is no gap between top and bottom.
    SHOULDER_Y = 215          # top of torso
    WAIST_Y = 470             # where top ends and bottom begins (overlap point)
    PANTS_BOTTOM_Y = 930      # ankle line
    HEAD_CENTER_Y = 112

    side_count = 0
    # Draw order: lower first, then upper, then outer on top.
    order = {"lower": 0, "upper": 1, "outer": 2, "feet": 0, "head": 0, "side": 0}
    for piece in sorted(pieces, key=lambda p: order.get(_classify(p), 0)):
        zone = _classify(piece)
        img = _load_cutout(piece.image_rel_path)
        if img is None:
            continue

        if zone in ("upper", "outer"):
            # Top: fill torso width; anchor BOTTOM just below the waist.
            target_w = 300 if zone == "upper" else 350
            fitted = _fit_to_width(img, target_w, max_h=360)
            _paste_anchored(canvas, fitted, cx, WAIST_Y + 25, anchor="bottom")
        elif zone == "lower":
            # Pants: fill the leg width like the top; anchor TOP at the waist so
            # it overlaps the top's hem. Cap height to reach the ankle line.
            fitted = _fit_to_width(img, 250, max_h=PANTS_BOTTOM_Y - WAIST_Y + 50)
            _paste_anchored(canvas, fitted, cx, WAIST_Y - 10, anchor="top")
        elif zone == "feet":
            # Shoes: anchor top near the ankle (slightly up to avoid a gap).
            fitted = _fit(img, 220, 150)
            _paste_anchored(canvas, fitted, cx, PANTS_BOTTOM_Y - 35, anchor="top")
        elif zone == "head":
            fitted = _fit(img, 170, 150)
            _paste_center(canvas, fitted, cx, HEAD_CENTER_Y)
        else:  # side panel, stacked
            scx = int(SIDE_ZONE_X * CANVAS_W)
            scy = int(0.22 * CANVAS_H) + side_count * 150
            fitted = _fit(img, 170, 130)
            _paste_center(canvas, fitted, scx, scy)
            side_count += 1

    # Caption
    d = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("arial.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    label = "Pratinjau Outfit"
    d.rectangle([0, CANVAS_H - 46, CANVAS_W, CANVAS_H], fill=(255, 255, 255, 235))
    d.text((20, CANVAS_H - 38), label, fill=(60, 60, 60), font=font)

    out = canvas.convert("RGB")
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return storage_service.save_generated_image(buf.getvalue(), ext=".png")
