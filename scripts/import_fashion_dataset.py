
import os
import time
import mimetypes

import pandas as pd
import requests


# ==================================================
# KONFIGURASI
# ==================================================

API_URL = "http://localhost:8000/api/v1/items"

# Folder tempat script dan CSV berada.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CSV_PATH = os.path.join(
    BASE_DIR,
    "sample_outfit_items.csv",
)

# Mapping nama kategori CSV ke category_id database.
CATEGORY_IDS = {
    "atasan": 1,
    "bawahan": 2,
    "sendal": 3,
    "sepatu": 4,
    "topi": 5,
    "kacamata": 6,
}

# True: lewati produk jika gambar tidak ditemukan.
SKIP_MISSING_IMAGE = True

# Jeda antar-request untuk mengurangi beban backend.
REQUEST_DELAY = 0.1


# ==================================================
# IMPORT DATA
# ==================================================

def main():
    if not os.path.exists(CSV_PATH):
        print(f"[ERROR] CSV tidak ditemukan: {CSV_PATH}")
        return

    # Baca CSV.
    df = pd.read_csv(CSV_PATH)

    required_columns = {
        "name",
        "category",
        "price",
        "image_path",
        "description",
    }

    missing_columns = required_columns - set(df.columns)

    if missing_columns:
        print(f"[ERROR] Kolom CSV kurang: {missing_columns}")
        return

    success = 0
    failed = 0
    skipped = 0

    # Session digunakan kembali untuk setiap request.
    with requests.Session() as session:
        for index, row in df.iterrows():
            name = str(row["name"]).strip()
            category = str(row["category"]).strip().lower()
            description = str(row["description"]).strip()

            # Validasi kategori.
            category_id = CATEGORY_IDS.get(category)

            if category_id is None:
                print(
                    f"[SKIP] Baris {index + 2}: "
                    f"kategori tidak dikenal: {category}"
                )
                skipped += 1
                continue

            # Pastikan harga berupa angka.
            try:
                price = float(row["price"])

                if price < 0:
                    raise ValueError("Harga tidak boleh negatif")

            except (ValueError, TypeError):
                print(
                    f"[SKIP] Baris {index + 2}: "
                    f"harga tidak valid"
                )
                skipped += 1
                continue

            # CSV dan gambar diasumsikan berada di folder yang sama.
            # Jika image_path diawali /, ambil nama filenya saja.
            image_name = os.path.basename(
                str(row["image_path"]).strip().replace("\\", "/")
            )

            image_file = os.path.join(BASE_DIR, image_name)

            if not os.path.isfile(image_file):
                message = (
                    f"[MISSING IMAGE] Baris {index + 2}: "
                    f"{image_file}"
                )
                print(message)

                if SKIP_MISSING_IMAGE:
                    skipped += 1
                    continue

            # Tentukan MIME type gambar berdasarkan ekstensi.
            mime_type, _ = mimetypes.guess_type(image_file)

            if not mime_type:
                mime_type = "application/octet-stream"

            try:
                # Buka gambar dalam binary mode.
                with open(image_file, "rb") as image:
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
                                image_name,
                                image,
                                mime_type,
                            )
                        },
                        timeout=60,
                    )

                if response.ok:
                    success += 1
                    print(
                        f"[OK] {name} | "
                        f"category_id={category_id} | "
                        f"price={price:g}"
                    )
                else:
                    failed += 1
                    print(
                        f"[API ERROR] {name} | "
                        f"HTTP {response.status_code} | "
                        f"{response.text[:300]}"
                    )

            except requests.RequestException as exc:
                failed += 1
                print(f"[REQUEST ERROR] {name}: {exc}")

            except OSError as exc:
                failed += 1
                print(f"[FILE ERROR] {image_file}: {exc}")

            time.sleep(REQUEST_DELAY)

    print("\n========== HASIL IMPORT ==========")
    print(f"Total baris CSV : {len(df)}")
    print(f"Berhasil        : {success}")
    print(f"Gagal           : {failed}")
    print(f"Dilewati        : {skipped}")
    print("==================================")


if __name__ == "__main__":
    main()
