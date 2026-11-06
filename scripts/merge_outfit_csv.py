"""Gabungkan rekomendasi_outfit.csv dan table-data.csv menjadi satu CSV
berformat sama: name, category, price, image_path, description.

- table-data.csv sudah berformat tersebut, hanya kategorinya dirapikan.
- rekomendasi_outfit.csv (id, nama_item, kategori, deskripsi) dikonversi:
  kategori dipetakan ke kategori database, harga diisi simulasi, dan
  image_path dicari dari nama file gambar di folder data.

Jalankan:
    python scripts/merge_outfit_csv.py
"""
import csv
import difflib
import os
import random
import re

ROOT = r"C:\Folder Kuliah\Semester 5\KECERDASAN BUATAN"
DATA_DIR = os.path.join(ROOT, "rekomendasi_outfit")
SOURCE_OUTFIT = os.path.join(DATA_DIR, "rekomendasi_outfit.csv")
SOURCE_TABLE = os.path.join(ROOT, "table-data.csv")
OUTPUT = os.path.join(DATA_DIR, "outfit_items.csv")

COLUMNS = ["name", "category", "price", "image_path", "description"]

# Kategori sumber -> kategori database (ditulis seperti di table-data.csv).
CATEGORY_MAP = {
    "atasan": "Atasan",
    "bawahan": "Bawahan",
    "outerwear": "Luaran",
    "dress": "Dress",
    "topi": "Topi",
    "kacamata": "Kacamata",
}
SANDAL_WORDS = ("sendal", "sandal")

# rekomendasi_outfit.csv tidak memuat harga: diisi acak (simulasi) per
# kategori, kelipatan Rp5.000. Seed dari nama produk -> hasil selalu sama.
PRICE_RANGES = {
    "Atasan": (60_000, 250_000),
    "Bawahan": (80_000, 300_000),
    "Luaran": (150_000, 450_000),
    "Dress": (120_000, 400_000),
    "Sendal": (30_000, 150_000),
    "Sepatu": (100_000, 500_000),
}

MIN_NAME_SIMILARITY = 0.85


def map_category(category: str, name: str) -> str | None:
    cat = category.strip().lower()
    if cat == "alas kaki":
        return "Sendal" if any(w in name.lower() for w in SANDAL_WORDS) else "Sepatu"
    return CATEGORY_MAP.get(cat)


def simulated_price(name: str, category: str) -> int:
    low, high = PRICE_RANGES[category]
    return random.Random(name).randrange(low, high + 1, 5_000)


def _name_key(text: str) -> str:
    """Kata-kata huruf kecil yang diurutkan, agar urutan kata tidak berpengaruh."""
    return " ".join(sorted(re.findall(r"[a-z]+", text.lower())))


def match_images(names: list[str], files: list[str]) -> dict[str, str]:
    """Pasangkan nama produk dengan file gambar: cocok persis (urutan kata
    bebas) dulu, lalu berdasarkan kemiripan untuk nama file yang salah ketik."""
    by_key = {_name_key(os.path.splitext(f)[0]): f for f in files}
    result = {n: by_key[_name_key(n)] for n in names if _name_key(n) in by_key}

    remaining = [f for f in files if f not in result.values()]
    for name in names:
        if name in result or not remaining:
            continue
        score, best = max(
            (difflib.SequenceMatcher(None, _name_key(name), _name_key(os.path.splitext(f)[0])).ratio(), f)
            for f in remaining
        )
        if score >= MIN_NAME_SIMILARITY:
            result[name] = best
            remaining.remove(best)
            print(f"[MATCH] {name!r} -> {best!r} (kemiripan {score:.2f})")
    return result


def read_csv(path: str) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def main() -> None:
    table_rows = read_csv(SOURCE_TABLE)
    outfit_rows = read_csv(SOURCE_OUTFIT)

    # Gambar milik table-data.csv tidak boleh dipasangkan ke produk lain.
    table_images = {r["image_path"].strip() for r in table_rows}
    files = [
        f for f in os.listdir(DATA_DIR)
        if os.path.isfile(os.path.join(DATA_DIR, f))
        and not f.lower().endswith(".csv") and f not in table_images
    ]
    names = [r["nama_item"].strip() for r in outfit_rows]
    images = match_images(names, files)

    merged: list[dict] = []
    problems: list[str] = []

    for r in outfit_rows:
        name = r["nama_item"].strip()
        category = map_category(r["kategori"], name)
        if category is None:
            problems.append(f"kategori tidak dikenal untuk {name!r}: {r['kategori']}")
            continue
        if name not in images:
            problems.append(f"gambar tidak ditemukan untuk {name!r}")
            continue
        merged.append({
            "name": name,
            "category": category,
            "price": simulated_price(name, category),
            "image_path": images[name],
            "description": r["deskripsi"].strip(),
        })

    for r in table_rows:
        name = r["name"].strip()
        category = map_category(r["category"], name)
        if category is None:
            problems.append(f"kategori tidak dikenal untuk {name!r}: {r['category']}")
            continue
        image = r["image_path"].strip()
        if not os.path.isfile(os.path.join(DATA_DIR, image)):
            problems.append(f"gambar tidak ditemukan untuk {name!r}: {image}")
            continue
        merged.append({
            "name": name,
            "category": category,
            "price": int(float(r["price"])),
            "image_path": image,
            "description": r["description"].strip(),
        })

    seen: set[str] = set()
    for row in merged:
        key = row["name"].lower()
        if key in seen:
            problems.append(f"nama ganda: {row['name']!r}")
        seen.add(key)

    if problems:
        print("\n[ERROR] CSV tidak ditulis karena ada masalah:")
        for p in problems:
            print(" -", p)
        return

    with open(OUTPUT, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(merged)

    print(f"\nTersimpan: {OUTPUT}")
    print(f"Total {len(merged)} produk "
          f"({len(outfit_rows)} dari rekomendasi_outfit.csv + {len(table_rows)} dari table-data.csv)")


if __name__ == "__main__":
    main()
