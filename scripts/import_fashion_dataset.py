"""Import katalog dari outfit_items.csv ke backend melalui POST /api/v1/items.

Format CSV: name, category, price, image_path, description. CSV ini dibuat
oleh scripts/merge_outfit_csv.py; image_path adalah nama file di DATA_DIR.
Backend harus sudah berjalan di API_BASE.

Jalankan:
    python scripts/import_fashion_dataset.py            # import
    python scripts/import_fashion_dataset.py --dry-run  # cek saja, tanpa kirim
"""
import io
import mimetypes
import os
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

CSV_PATH = os.path.join(DATA_DIR, "outfit_items.csv")

# Kategori -> body_zone. Kategori yang belum ada di database dibuat otomatis
# lewat POST /api/v1/categories.
BODY_ZONES = {
    "atasan": "upper",
    "bawahan": "lower",
    "luaran": "outer",
    "dress": "upper",
    "sendal": "feet",
    "sepatu": "feet",
    "topi": "accessory",
    "kacamata": "accessory",
}

# Format gambar yang diterima backend; format lain (mis. .avif) dikonversi.
ACCEPTED_EXT = {".jpg", ".jpeg", ".png", ".webp"}

# Jeda antar-request untuk mengurangi beban backend.
REQUEST_DELAY = 0.1


# ==================================================
# HELPER
# ==================================================

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


def get_category_ids(session: requests.Session, needed: set[str], dry_run: bool) -> dict[str, int]:
    """Ambil ID kategori dari backend; buat kategori yang belum ada."""
    response = session.get(f"{API_BASE}/categories", timeout=30)
    response.raise_for_status()
    ids = {c["name"].lower(): c["id"] for c in response.json()}

    for name in sorted(needed):
        if name in ids:
            continue
        zone = BODY_ZONES[name]
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
        print("        Buat dulu dengan: python scripts/merge_outfit_csv.py")
        return

    df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")

    required_columns = {"name", "category", "price", "image_path", "description"}
    missing_columns = required_columns - set(df.columns)
    if missing_columns:
        print(f"[ERROR] Kolom CSV kurang: {missing_columns}")
        return

    categories = {str(c).strip().lower() for c in df["category"]}
    unknown = categories - set(BODY_ZONES)
    if unknown:
        print(f"[ERROR] Kategori tanpa body_zone di BODY_ZONES: {sorted(unknown)}")
        return

    success = failed = skipped = 0

    with requests.Session() as session:
        try:
            category_ids = get_category_ids(session, categories, dry_run)
            existing = session.get(f"{API_BASE}/items", timeout=60)
            existing.raise_for_status()
            existing_names = {it["name"].strip().lower() for it in existing.json()}
        except requests.RequestException as exc:
            print(f"[ERROR] Backend tidak dapat diakses di {API_BASE}: {exc}")
            return

        for index, row in df.iterrows():
            line = index + 2
            name = str(row["name"]).strip()
            category = str(row["category"]).strip().lower()

            if name.lower() in existing_names:
                print(f"[SKIP] Baris {line}: '{name}' sudah ada di katalog")
                skipped += 1
                continue

            try:
                price = float(row["price"])
                if price < 0:
                    raise ValueError
            except (ValueError, TypeError):
                print(f"[SKIP] Baris {line}: harga tidak valid: {row['price']}")
                skipped += 1
                continue

            image_file = os.path.join(DATA_DIR, os.path.basename(str(row["image_path"]).strip()))
            if not os.path.isfile(image_file):
                print(f"[MISSING IMAGE] Baris {line}: {image_file}")
                skipped += 1
                continue

            description = with_gender(name, str(row["description"]).strip())

            try:
                data, upload_name, mime_type = load_image(image_file)
            except OSError as exc:
                print(f"[FILE ERROR] {image_file}: {exc}")
                failed += 1
                continue

            if dry_run:
                print(f"[DRY RUN] {name} | {category} | Rp{price:,.0f} | {upload_name}".replace(",", "."))
                success += 1
                continue

            try:
                response = session.post(
                    f"{API_BASE}/items",
                    data={
                        "name": name,
                        "category_id": str(category_ids[category]),
                        "price": str(int(price)),
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
                print(f"[OK] {name} | {category} | price={price:g}")
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
