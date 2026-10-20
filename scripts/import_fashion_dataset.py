
import io
import random
import time

import requests
import torch
from datasets import load_dataset
from PIL import Image
from ultralytics import YOLO
from transformers import pipeline


# =========================================================
# KONFIGURASI
# =========================================================

API_URL = "http://localhost:8000/api/v1/items"
DATASET_NAME = "ashraq/fashion-product-images-small"

# Maksimal jumlah item yang berhasil diinsert per kategori.
MAX_PER_CATEGORY = 20

# None = periksa seluruh dataset sampai semua target terpenuhi.
MAX_ROWS_TO_SCAN = None

# Confidence YOLO untuk mendeteksi manusia.
PERSON_CONFIDENCE = 0.10

# Filter CLIP:
# label produk harus memiliki skor minimal ini dan unggul
# dari label manusia/manekin dengan selisih minimum.
CLIP_MIN_SCORE = 0.35
CLIP_MIN_MARGIN = 0.08

REQUEST_DELAY = 0.1

CATEGORY_IDS = {
    "atasan": 1,
    "bawahan": 2,
    "sendal": 3,
    "sepatu": 4,
    "topi": 5,
    "kacamata": 6,
}

# Harga simulasi, bukan harga asli toko.
PRICE_RANGES = {
    1: (50000, 250000),
    2: (60000, 300000),
    3: (30000, 150000),
    4: (80000, 500000),
    5: (20000, 120000),
    6: (25000, 200000),
}


# =========================================================
# LABEL UNTUK FILTER CLIP
# =========================================================

PRODUCT_LABELS = [
    "a single isolated clothing product photographed alone "
    "on a plain white background, no person",

    "a single isolated pair of shoes or sandals photographed "
    "alone on a plain white background, no person",

    "a single isolated hat or pair of sunglasses photographed "
    "alone on a plain white background, no person",
]

NON_PRODUCT_LABELS = [
    "a photograph of a person wearing clothes",
    "a fashion model wearing a shirt and trousers",
    "a cropped photograph of human legs wearing trousers",
    "a cropped photograph of a human torso wearing a shirt",
    "a mannequin wearing clothing",
    "a person posing for a fashion catalog",
    "multiple clothing items worn together by a person",
]

ALL_LABELS = PRODUCT_LABELS + NON_PRODUCT_LABELS


# =========================================================
# LOAD MODEL
# =========================================================

print("Loading YOLO model...")
person_detector = YOLO("yolov8n.pt")

print("Loading CLIP model...")
device = 0 if torch.cuda.is_available() else -1

clip_classifier = pipeline(
    task="zero-shot-image-classification",
    model="openai/clip-vit-base-patch32",
    device=device,
)

print("Models loaded.")


# =========================================================
# CATEGORY MAPPING
# =========================================================

def detect_category(row):
    article = str(
        row.get("articleType") or ""
    ).strip().lower()

    sub = str(
        row.get("subCategory") or ""
    ).strip().lower()

    # Sandal diperiksa lebih dahulu.
    if article in {
        "sandals", "flip flops", "flip-flops", "slippers"
    }:
        return CATEGORY_IDS["sendal"]

    if article in {
        "sunglasses", "eyeglasses", "spectacles"
    }:
        return CATEGORY_IDS["kacamata"]

    if article in {"cap", "caps", "hat", "hats"}:
        return CATEGORY_IDS["topi"]

    if article in {
        "casual shoes", "formal shoes", "sports shoes",
        "heels", "flats", "boots"
    }:
        return CATEGORY_IDS["sepatu"]

    if article in {
        "jeans", "trousers", "shorts", "skirts",
        "track pants", "leggings", "tights",
        "churidar", "salwar"
    }:
        return CATEGORY_IDS["bawahan"]

    if article in {
        "shirts", "tshirts", "t-shirts", "tops",
        "sweatshirts", "sweaters", "hoodies",
        "blouses", "polos", "kurtas", "jackets"
    }:
        return CATEGORY_IDS["atasan"]

    # Fallback berdasarkan metadata kategori.
    if sub == "topwear":
        return CATEGORY_IDS["atasan"]

    return None


# =========================================================
# FILTER YOLO
# =========================================================

def contains_person(image):
    """
    Menolak gambar ketika YOLO mendeteksi objek person.

    Jika deteksi gagal, gambar ditolak untuk menghindari
    upload gambar yang belum berhasil diperiksa.
    """
    if image is None:
        return True

    try:
        results = person_detector.predict(
            source=image.convert("RGB"),
            classes=[0],
            conf=PERSON_CONFIDENCE,
            imgsz=1280,
            verbose=False,
        )

        for result in results:
            if (
                result.boxes is not None
                and len(result.boxes) > 0
            ):
                return True

        return False

    except Exception as exc:
        print(f"[YOLO ERROR] {exc}")
        return True


# =========================================================
# FILTER CLIP
# =========================================================

def is_product_only(image):
    """
    Menilai apakah gambar lebih cocok sebagai foto produk
    tunggal dibandingkan foto manusia atau manekin.

    Skor CLIP bukan probabilitas terkalibrasi.
    Filter ini membantu, tetapi tidak menjamin akurasi 100%.
    """
    if image is None:
        return False

    try:
        results = clip_classifier(
            image.convert("RGB"),
            candidate_labels=ALL_LABELS,
        )

        scores = {
            result["label"]: result["score"]
            for result in results
        }

        product_score = max(
            scores.get(label, 0.0)
            for label in PRODUCT_LABELS
        )

        non_product_score = max(
            scores.get(label, 0.0)
            for label in NON_PRODUCT_LABELS
        )

        accepted = (
            product_score >= CLIP_MIN_SCORE
            and product_score - non_product_score
            >= CLIP_MIN_MARGIN
        )

        if not accepted:
            top_label = max(
                scores,
                key=scores.get,
            )

            print(
                f"[CLIP SKIP] label={top_label!r}, "
                f"product={product_score:.3f}, "
                f"non_product={non_product_score:.3f}"
            )

        return accepted

    except Exception as exc:
        print(f"[CLIP ERROR] {exc}")
        return False


# =========================================================
# GAMBAR DAN METADATA
# =========================================================

def image_to_bytes(image):
    """
    Mengubah gambar ke PNG.

    Catatan: konversi PNG tidak otomatis menghilangkan
    background putih dari gambar sumber.
    """
    if not isinstance(image, Image.Image):
        return None

    buffer = io.BytesIO()

    try:
        image.convert("RGBA").save(
            buffer,
            format="PNG",
        )
        buffer.seek(0)
        return buffer

    except Exception as exc:
        print(f"[IMAGE ERROR] {exc}")
        buffer.close()
        return None


def get_price(category_id):
    low, high = PRICE_RANGES[category_id]

    return random.randint(
        (low + 4999) // 5000,
        high // 5000,
    ) * 5000


def make_description(row):
    parts = []

    fields = [
        ("articleType", "Jenis"),
        ("baseColour", "Warna"),
        ("gender", "Gender"),
        ("usage", "Penggunaan"),
    ]

    for key, label in fields:
        value = row.get(key)

        if value is not None and str(value).strip():
            parts.append(f"{label}: {str(value).strip()}")

    parts.append("Harga estimasi untuk katalog demo.")

    return ". ".join(parts)


# =========================================================
# UPLOAD API
# =========================================================

def upload_item(session, row, category_id, image):
    buffer = image_to_bytes(image)

    if buffer is None:
        return False

    name = str(
        row.get("productDisplayName")
        or row.get("articleType")
        or f"Fashion item {row.get('id', 'unknown')}"
    ).strip()[:255]

    description = make_description(row)
    price = get_price(category_id)

    try:
        response = session.post(
            API_URL,
            data={
                "name": name,
                "category_id": str(category_id),
                "price": str(price),
                "description": description,
            },
            files={
                "image": (
                    f"fashion_{row.get('id', 'item')}.png",
                    buffer,
                    "image/png",
                )
            },
            timeout=60,
        )

        if response.ok:
            print(
                f"[INSERTED] {name} | "
                f"category_id={category_id} | "
                f"price={price}"
            )
            return True

        print(
            f"[API ERROR] HTTP {response.status_code}: "
            f"{response.text[:300]}"
        )
        return False

    except requests.RequestException as exc:
        print(f"[REQUEST ERROR] {name}: {exc}")
        return False

    finally:
        buffer.close()


# =========================================================
# MAIN
# =========================================================

def main():
    print(f"Loading dataset: {DATASET_NAME}")

    dataset = load_dataset(
        DATASET_NAME,
        split="train",
    )

    counts = {
        category_id: 0
        for category_id in CATEGORY_IDS.values()
    }

    scanned = 0
    success = 0
    failed = 0
    skipped_category = 0
    skipped_person = 0
    skipped_clip = 0

    with requests.Session() as session:
        for row in dataset:
            if (
                MAX_ROWS_TO_SCAN is not None
                and scanned >= MAX_ROWS_TO_SCAN
            ):
                print("Scan limit reached.")
                break

            # Berhenti jika semua kategori mencapai target.
            if all(
                count >= MAX_PER_CATEGORY
                for count in counts.values()
            ):
                break

            scanned += 1

            category_id = detect_category(row)

            if category_id is None:
                skipped_category += 1
                continue

            if counts[category_id] >= MAX_PER_CATEGORY:
                continue

            image = row.get("image")

            if not isinstance(image, Image.Image):
                failed += 1
                print(f"[SKIP] Invalid image: {row.get('id')}")
                continue

            # Tahap pertama: deteksi manusia.
            if contains_person(image):
                skipped_person += 1
                print(
                    f"[YOLO SKIP] "
                    f"{row.get('productDisplayName') or row.get('id')}"
                )
                continue

            # Tahap kedua: klasifikasi gambar produk.
            if not is_product_only(image):
                skipped_clip += 1
                continue

            # Upload hanya jika kedua filter lolos.
            uploaded = upload_item(
                session,
                row,
                category_id,
                image,
            )

            if uploaded:
                counts[category_id] += 1
                success += 1
            else:
                failed += 1

            time.sleep(REQUEST_DELAY)

    print("\n========== IMPORT SUMMARY ==========")
    print("Scanned          :", scanned)
    print("Inserted         :", success)
    print("Failed           :", failed)
    print("Skipped category :", skipped_category)
    print("Skipped YOLO     :", skipped_person)
    print("Skipped CLIP     :", skipped_clip)

    for name, category_id in CATEGORY_IDS.items():
        print(
            f"{name} (id={category_id}): "
            f"{counts[category_id]}/{MAX_PER_CATEGORY}"
        )

    print("====================================")


if __name__ == "__main__":
    main()
