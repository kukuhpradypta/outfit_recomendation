"""Import katalog dari rekomendasi_outfit.csv ke backend melalui POST /api/v1/items.

Format CSV: id, nama_item, kategori, deskripsi. Gambar berada di folder yang
sama dengan CSV dan namanya mengikuti nama_item (boleh beda urutan kata atau
salah ketik sedikit). Backend harus sudah berjalan di API_BASE.

Jalankan:
    python scripts/import_fashion_dataset.py            # import
    python scripts/import_fashion_dataset.py --dry-run  # cek saja, tanpa kirim
"""
import difflib
import io
import mimetypes
import os
import random
import re
import sys
import time

import pandas as pd
import requests
from PIL import Image


# ==================================================
# KONFIGURASI
# ==================================================

API_BASE = "http://localhost:8000/api/v1"

# Folder tempat CSV dan gambar produk berada.
DATA_DIR = r"C:\Folder Kuliah\Semester 5\KECERDASAN BUATAN\rekomendasi_outfit"

CSV_PATH = os.path.join(DATA_DIR, "rekomendasi_outfit.csv")

# Kategori CSV -> (nama kategori di database, body_zone).
# Kategori yang belum ada dibuat otomatis lewat POST /api/v1/categories.
# "Alas Kaki" dipisah menjadi sendal atau sepatu berdasarkan nama produk.
CATEGORY_MAP = {
    "atasan": ("atasan", "upper"),
    "bawahan": ("bawahan", "lower"),
    "outerwear": ("luaran", "outer"),
    "dress": ("dress", "upper"),
}
SANDAL_WORDS = ("sendal", "sandal")

# CSV tidak memuat harga, jadi harga diisi acak (simulasi) per kategori dan
# dibulatkan ke kelipatan Rp5.000. Seed dari nama produk membuat harga tetap
# sama setiap kali script dijalankan ulang.
PRICE_RANGES = {
    "atasan": (60_000, 250_000),
    "bawahan": (80_000, 300_000),
    "luaran": (150_000, 450_000),
    "dress": (120_000, 400_000),
    "sendal": (30_000, 150_000),
    "sepatu": (100_000, 500_000),
}

# Format gambar yang diterima backend; format lain (mis. .avif) dikonversi.
ACCEPTED_EXT = {".jpg", ".jpeg", ".png", ".webp"}

# Nilai minimal kemiripan nama file dengan nama_item (0-1).
MIN_NAME_SIMILARITY = 0.85

# Jeda antar-request untuk mengurangi beban backend.
REQUEST_DELAY = 0.1


# ==================================================
# HELPER
# ==================================================

def _name_key(text: str) -> str:
    """Kata-kata huruf kecil yang diurutkan, agar urutan kata tidak berpengaruh."""
    return " ".join(sorted(re.findall(r"[a-z]+", text.lower())))


def match_images(names: list[str], files: list[str]) -> dict[str, str]:
    """Pasangkan setiap nama produk dengan satu file gambar.

    Pertama dicocokkan persis (urutan kata bebas), lalu sisanya dicocokkan
    berdasarkan kemiripan untuk menangani salah ketik pada nama file.
    """
    by_key = {_name_key(os.path.splitext(f)[0]): f for f in files}
    result: dict[str, str] = {}
    for name in names:
        f = by_key.get(_name_key(name))
        if f:
            result[name] = f

    remaining = [f for f in files if f not in result.values()]
    for name in names:
        if name in result or not remaining:
            continue
        scored = [
            (difflib.SequenceMatcher(None, _name_key(name), _name_key(os.path.splitext(f)[0])).ratio(), f)
            for f in remaining
        ]
        score, best = max(scored)
        if score >= MIN_NAME_SIMILARITY:
            result[name] = best
            remaining.remove(best)
            print(f"[MATCH] {name!r} -> {best!r} (kemiripan {score:.2f})")
    return result


def resolve_category(csv_category: str, name: str) -> tuple[str, str] | None:
    cat = csv_category.strip().lower()
    if cat == "alas kaki":
        is_sandal = any(w in name.lower() for w in SANDAL_WORDS)
        return ("sendal", "feet") if is_sandal else ("sepatu", "feet")
    return CATEGORY_MAP.get(cat)


def simulated_price(name: str, category: str) -> int:
    low, high = PRICE_RANGES[category]
    rng = random.Random(name)
    return rng.randrange(low, high + 1, 5_000)


def with_gender(name: str, description: str) -> str:
    """Tambahkan 'Gender: Men/Women' agar backend dapat menjaga konsistensi gender."""
    lower = name.lower()
    if "wanita" in lower:
        return f"{description} Gender: Women."
    if "pria" in lower:
        return f"{description} Gender: Men."
    return description


def load_image(path: str) -> tuple[bytes, str, str]:
    """Kembalikan (bytes, nama_file, mime). Format yang tidak diterima dikonversi ke WEBP."""
    filename = os.path.basename(path)
    ext = os.path.splitext(filename)[1].lower()
    if ext in ACCEPTED_EXT:
        with open(path, "rb") as fh:
            data = fh.read()
        mime = mimetypes.guess_type(filename)[0] or "image/jpeg"
        return data, filename, mime

    with Image.open(path) as im:
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="WEBP", quality=90)
    return buf.getvalue(), os.path.splitext(filename)[0] + ".webp", "image/webp"


def get_category_ids(session: requests.Session, needed: dict[str, str], dry_run: bool) -> dict[str, int]:
    """Ambil ID kategori dari backend; buat kategori yang belum ada."""
    response = session.get(f"{API_BASE}/categories", timeout=30)
    response.raise_for_status()
    ids = {c["name"].lower(): c["id"] for c in response.json()}

    for name, zone in needed.items():
        if name in ids:
            continue
        if dry_run:
            print(f"[DRY RUN] Kategori baru akan dibuat: {name} (body_zone={zone})")
            ids[name] = -1
            continue
        created = session.post(
            f"{API_BASE}/categories", json={"name": name, "body_zone": zone}, timeout=30
        )
        created.raise_for_status()
        ids[name] = created.json()["id"]
        print(f"[KATEGORI] Dibuat: {name} (body_zone={zone}, id={ids[name]})")
    return ids


# ==================================================
# IMPORT DATA
# ==================================================

def main(dry_run: bool = False):
    if not os.path.exists(CSV_PATH):
        print(f"[ERROR] CSV tidak ditemukan: {CSV_PATH}")
        return

    df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")

    required_columns = {"nama_item", "kategori", "deskripsi"}
    missing_columns = required_columns - set(df.columns)
    if missing_columns:
        print(f"[ERROR] Kolom CSV kurang: {missing_columns}")
        return

    csv_name = os.path.basename(CSV_PATH)
    files = [
        f for f in os.listdir(DATA_DIR)
        if f != csv_name and os.path.isfile(os.path.join(DATA_DIR, f))
    ]
    names = [str(n).strip() for n in df["nama_item"]]
    images = match_images(names, files)

    success = failed = skipped = 0

    with requests.Session() as session:
        try:
            needed = {}
            for _, row in df.iterrows():
                resolved = resolve_category(str(row["kategori"]), str(row["nama_item"]))
                if resolved:
                    needed[resolved[0]] = resolved[1]
            category_ids = get_category_ids(session, needed, dry_run)

            existing = session.get(f"{API_BASE}/items", timeout=60)
            existing.raise_for_status()
            existing_names = {it["name"].strip().lower() for it in existing.json()}
        except requests.RequestException as exc:
            print(f"[ERROR] Backend tidak dapat diakses di {API_BASE}: {exc}")
            return

        for index, row in df.iterrows():
            line = index + 2
            name = str(row["nama_item"]).strip()
            description = str(row["deskripsi"]).strip()

            resolved = resolve_category(str(row["kategori"]), name)
            if resolved is None:
                print(f"[SKIP] Baris {line}: kategori tidak dikenal: {row['kategori']}")
                skipped += 1
                continue
            category, _ = resolved

            if name.lower() in existing_names:
                print(f"[SKIP] Baris {line}: '{name}' sudah ada di katalog")
                skipped += 1
                continue

            image_name = images.get(name)
            if image_name is None:
                print(f"[MISSING IMAGE] Baris {line}: tidak ada gambar untuk '{name}'")
                skipped += 1
                continue

            price = simulated_price(name, category)
            description = with_gender(name, description)

            try:
                data, upload_name, mime_type = load_image(os.path.join(DATA_DIR, image_name))
            except OSError as exc:
                print(f"[FILE ERROR] {image_name}: {exc}")
                failed += 1
                continue

            if dry_run:
                print(f"[DRY RUN] {name} | {category} | Rp{price:,} | {upload_name}".replace(",", "."))
                success += 1
                continue

            try:
                response = session.post(
                    f"{API_BASE}/items",
                    data={
                        "name": name,
                        "category_id": str(category_ids[category]),
                        "price": str(price),
                        "description": description,
                    },
                    files={"image": (upload_name, data, mime_type)},
                    timeout=60,
                )
            except requests.RequestException as exc:
                failed += 1
                print(f"[REQUEST ERROR] {name}: {exc}")
                continue

            if response.ok:
                success += 1
                print(f"[OK] {name} | {category} | price={price}")
            else:
                failed += 1
                print(f"[API ERROR] {name} | HTTP {response.status_code} | {response.text[:300]}")

            time.sleep(REQUEST_DELAY)

    print("\n========== HASIL IMPORT ==========")
    print(f"Mode            : {'DRY RUN (tidak ada data dikirim)' if dry_run else 'IMPORT'}")
    print(f"Total baris CSV : {len(df)}")
    print(f"Berhasil        : {success}")
    print(f"Gagal           : {failed}")
    print(f"Dilewati        : {skipped}")
    print("==================================")


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
