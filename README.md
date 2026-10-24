# AI Outfit Recommendation Backend

Backend API untuk merekomendasikan outfit berdasarkan katalog produk toko, foto
pengguna/mannequin, estimasi budget, dan prompt gaya opsional. AI hanya memilih
produk yang benar-benar ada di database, menjelaskan alasannya, lalu outfit
divisualisasikan pada manekin atau foto pengguna.

## Peran Model AI (Gemini)
Inti kecerdasan ada pada penalaran, bukan pembuatan gambar. Gemini Flash dipakai
untuk tiga hal (semuanya berjalan di free tier):
1. **Analisis foto (Gemini Vision)** — membaca foto pengguna/mannequin untuk
   memperkaya rekomendasi (konteks, tone warna kulit).
2. **Pemilihan produk** — memilih kombinasi outfit dari katalog nyata sesuai
   budget, gaya, dan hasil analisis foto. Bukan sekadar filter harga.
3. **Penjelasan** — menghasilkan alasan natural language untuk outfit terpilih.

Visualisasi outfit (manekin / foto) dibuat secara **lokal dengan Pillow**
(gratis, tanpa billing), karena model image Gemini punya kuota 0 di free tier.
Jadi Gemini berperan sebagai "otak" (memilih & menjelaskan), sedangkan Pillow
hanya alat menggambar hasilnya.

## Tech Stack
- Python 3.12
- FastAPI + Uvicorn
- PostgreSQL + SQLAlchemy 2.0
- Gemini API (`google-genai`) — Flash untuk vision, pemilihan produk, dan penjelasan
- Pillow — komposit visual outfit (manekin / foto), gratis & lokal
- Scalar API Reference untuk dokumentasi interaktif (di `/docs`)

## Struktur Proyek
```
.
├── main.py                       # Entry point FastAPI + Scalar + static files
├── app/
│   ├── config.py                 # Konfigurasi dari .env
│   ├── database.py               # Engine, session, init tabel
│   ├── models.py                 # ORM: tabel `category` & `item`
│   ├── routers/
│   │   ├── items.py              # POST/GET /api/v1/items
│   │   ├── outfits.py            # POST /api/v1/outfits/try-on
│   │   └── recommendations.py    # POST /api/v1/recommendations
│   ├── schemas/                  # Pydantic request/response
│   ├── repositories/             # Akses data `item`
│   └── services/
│       ├── gemini_client.py      # Wrapper google-genai (retry + model ID dari env)
│       ├── storage_service.py    # Simpan & validasi gambar di public/
│       ├── composite_service.py  # Komposit outfit lokal (Pillow)
│       └── recommendation_service.py  # Orkestrasi: analisis → pilih → jelaskan → visual
├── migrations/                   # Alembic (env.py, versions/)
├── alembic.ini                   # Konfigurasi Alembic
├── scripts/
│   ├── seed_categories.py        # Seeder kategori default
│   └── generate_doc.py           # Generator dokumen konsep (.docx)
├── public/images/items/          # Gambar produk
├── public/images/generated/      # Hasil pratinjau outfit
├── requirements.txt
├── .env.example
└── README.md
```

Tiga tahap pada `recommendation_service.py` sengaja dipisah agar mudah
dikembangkan: `select_products` (pemilihan produk), `explain` (penjelasan), dan
`generate_try_on` (image editing).

## Prasyarat
- Python 3.12 (contoh menggunakan conda env `py312`)
- PostgreSQL berjalan dan dapat diakses
- Gemini API key

## Instalasi
```bash
conda activate py312          # atau buat venv sendiri
pip install -r requirements.txt
```

## Konfigurasi Database & Environment
1. Salin `.env.example` menjadi `.env` lalu sesuaikan nilainya:
   ```bash
   cp .env.example .env
   ```
2. Isi kredensial PostgreSQL (`PG_HOST`, `PG_PORT`, `PG_USER`, `PG_PASSWORD`,
   `PG_DB_NAME`) dan `GEMINI_API_KEY`.
3. Buat database jika belum ada (ganti nama sesuai `PG_DB_NAME`):
   ```sql
   CREATE DATABASE outfit_recomendation;
   ```
   Tabel dikelola oleh Alembic (lihat bagian Migration), bukan `create_all`.

### Migration (Alembic)
Skema database dikelola dengan Alembic. Setelah database dibuat, jalankan:
```bash
alembic upgrade head        # buat/terapkan semua tabel
```
Perintah berguna lainnya:
```bash
alembic current             # lihat revisi aktif
alembic history             # riwayat migration
alembic downgrade -1        # mundur satu langkah
alembic revision --autogenerate -m "pesan"   # buat migration dari perubahan model
```
URL database diambil otomatis dari `.env` (via `app.config`), jadi tidak ada
kredensial di `alembic.ini`.

### Seeder Kategori
Isi kategori default (atasan, bawahan, sendal, sepatu, topi, kacamata) beserta
`body_zone`-nya. Idempoten, aman dijalankan berulang:
```bash
python scripts/seed_categories.py
```

### Skema Database
- `category`: `id`, `name` (unik), `body_zone` (upper/lower/feet/outer/accessory),
  `created_at`. `body_zone` menentukan posisi item pada pratinjau manekin.
- `item`: `id`, `name`, `category_id` (FK ke `category`), `price`, `image_path`,
  `description`, `created_at`.

Buat kategori lebih dulu sebelum membuat item. Warna produk tidak disimpan di
kolom khusus; sebutkan warna di `name` atau `description` agar AI dapat
mempertimbangkannya saat memilih outfit.

### Konfigurasi Gemini
- `GEMINI_API_KEY` wajib diisi.
- `GEMINI_TEXT_MODEL` (default `gemini-flash-latest`) — model reasoning/penjelasan.
- `GEMINI_IMAGE_MODEL` (default `gemini-2.5-flash-image`, "Nano Banana") — image editing.

Model ID tidak di-hardcode. Nama model Gemini bisa berubah/di-deprecate. Cek
model yang tersedia untuk key Anda:
```bash
curl "https://generativelanguage.googleapis.com/v1beta/models?key=YOUR_KEY"
```
Lalu set `GEMINI_TEXT_MODEL` ke ID yang didukung. `gemini-flash-latest` dipilih
sebagai default karena selalu menunjuk ke Flash stabil terbaru dan menghindari
error deprecation per-versi. Klien Gemini juga otomatis retry pada error
transient (429/500/503).

> Catatan: jika layanan AI sedang sibuk/tidak tersedia, pemilihan produk jatuh
> ke fallback berbasis budget (satu item per kategori termurah yang muat budget),
> sehingga API tetap mengembalikan outfit beserta pratinjau gambar.

> Visualisasi outfit dibuat lokal (Pillow), jadi tidak butuh kuota image Gemini.

## Menjalankan
Pastikan migration sudah diterapkan dan kategori sudah di-seed lebih dulu:
```bash
alembic upgrade head
python scripts/seed_categories.py
```
Lalu jalankan server:
```bash
python main.py
# atau
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

- Dokumentasi interaktif (Scalar): http://localhost:8000/docs
- OpenAPI JSON: http://localhost:8000/openapi.json
- Gambar statis: http://localhost:8000/public/images/...

Swagger UI & ReDoc bawaan FastAPI dinonaktifkan; `/docs` menampilkan Scalar.

## Endpoint

### `POST /api/v1/categories`
Membuat kategori baru (JSON). `name` harus unik.

```bash
curl -X POST http://localhost:8000/api/v1/categories \
  -H "Content-Type: application/json" \
  -d '{"name": "atasan", "body_zone": "upper"}'
```
`body_zone` salah satu dari: `upper`, `lower`, `feet`, `outer`, `accessory`.

### `GET /api/v1/categories`
Melihat semua kategori (berguna untuk mendapat `category_id`).

### `POST /api/v1/items`
Membuat produk baru (multipart/form-data). Buat kategori lebih dulu.

Field form: `name`, `category_id`, `price`, `description` (opsional, sebutkan
warna di sini agar AI akurat), `image` (file jpg/png/webp).

Contoh (harga mendukung format Indonesia `150.000`):
```bash
curl -X POST http://localhost:8000/api/v1/items \
  -F "name=Kemeja Linen Putih" \
  -F "category_id=1" \
  -F "price=150.000" \
  -F "description=Kemeja linen ringan" \
  -F "image=@/path/ke/gambar.jpg"
```

### `GET /api/v1/items` dan `GET /api/v1/items/{id}`
Melihat daftar atau satu item (menyertakan objek kategori).

### `POST /api/v1/recommendations`
Menghasilkan rekomendasi outfit (multipart/form-data).

Field form: `image` (foto pengguna/mannequin), `budget` (angka),
`prompt` (opsional, mis. "gaya formal, warna netral"), `mode` (opsional:
`mannequin` default, atau `user_photo`).

Contoh:
```bash
curl -X POST http://localhost:8000/api/v1/recommendations \
  -F "image=@/path/ke/foto.jpg" \
  -F "budget=700000" \
  -F "prompt=gaya kasual minimalis" \
  -F "mode=mannequin"
```

Contoh respons:
```json
{
  "items": [
    {"item": {"id": 1, "name": "Kemeja Putih", "price": 150000.0,
              "category": {"id": 1, "name": "atasan", "body_zone": "upper"},
              "image_path": "images/items/....png", "description": "...",
              "created_at": "..."},
     "reason": "Cocok untuk gaya kasual minimalis."}
  ],
  "total_price": 650000.0,
  "budget": 700000.0,
  "within_budget": true,
  "overall_reasoning": "Kombinasi ini ...",
  "photo_analysis": "Tone kulit hangat, cocok warna netral.",
  "mode": "mannequin",
  "generated_image_path": "images/generated/....png",
  "image_editing_error": null
}
```

Field `generated_image_path` berisi path pratinjau outfit (manekin atau foto).
Akses gambarnya via `http://localhost:8000/public/<generated_image_path>`.

### Refresh rekomendasi (minta opsi lain)
Setiap respons rekomendasi memuat field `combination` (daftar ID item, terurut).
Untuk meminta kombinasi lain, kirim ulang request dengan `refresh=true` dan
kombinasi yang sudah pernah ditampilkan di `previous_combinations`:
```bash
curl -X POST http://localhost:8000/api/v1/recommendations \
  -F "budget=1000000" \
  -F "prompt=outfit hangout gaya keren" \
  -F "mode=mannequin" \
  -F "refresh=true" \
  -F "previous_combinations=327,346,363" \
  -F "previous_combinations=327,350,363"
```
Satu nilai `previous_combinations` = satu outfit lengkap (`327,346,363`). Jika
setiap nilai hanya berisi satu ID (mis. field diulang per ID dari GUI), semua ID
itu dianggap satu kombinasi.

Hasil dijamin tidak sama persis dengan kombinasi mana pun di
`previous_combinations`. Sebagian item boleh sama (mis. bawahan sama, atasan
berbeda). Jika tidak ada kombinasi baru yang masuk budget, field `notice` berisi
penjelasannya. Maksimal 20 kombinasi; satu field juga boleh berisi beberapa
kombinasi yang dipisah `;`.

### `POST /api/v1/outfits/try-on`
Pratinjau outfit dari item pilihan pengguna (multipart/form-data). Isi minimal
satu dari: `atasan_id`, `bawahan_id`, `alas_kaki_id`, `kacamata_id`, `topi_id`,
ditambah `mode` (`mannequin`/`user_photo`) dan `image` (wajib untuk `user_photo`).

Validasi kategori per slot:
| Field | Kategori yang diterima |
|---|---|
| `atasan_id` | body_zone `upper`/`outer` (atasan, luaran) |
| `bawahan_id` | body_zone `lower` (bawahan) |
| `alas_kaki_id` | body_zone `feet` (sepatu dan sendal) |
| `kacamata_id` | kategori `kacamata` |
| `topi_id` | kategori `topi` |

```bash
curl -X POST http://localhost:8000/api/v1/outfits/try-on \
  -F "atasan_id=298" -F "bawahan_id=334" -F "alas_kaki_id=295" \
  -F "kacamata_id=314" -F "topi_id=342" -F "mode=mannequin"
```
Jika ID tidak ada atau kategorinya tidak sesuai slot, respons 422 berisi semua
kesalahan sekaligus:
```json
{"detail": {"message": "Validasi outfit gagal.", "errors": [
  {"field": "kacamata_id", "message": "Item 298 ('...') berkategori 'atasan', bukan kacamata. Kategori yang diterima: kacamata."}
]}}
```

## Performa
- Katalog di-shortlist lokal sebelum dikirim ke Gemini: maksimal
  `RECO_MAX_PER_CATEGORY` item per kategori, diurutkan dari relevansi kata kunci
  prompt dan tersebar di rentang harga. Format prompt ringkas (bukan JSON).
- Analisis foto, pemilihan produk, dan penjelasan dilakukan dalam **satu**
  panggilan Gemini, dengan `GEMINI_THINKING_LEVEL=low` dan batas waktu
  `GEMINI_TIMEOUT_SEC` per model. Jika model utama timeout atau sibuk
  (429/503/504), model di `GEMINI_FALLBACK_MODELS` dicoba berurutan, dan model
  yang gagal dilewati selama 5 menit. Jika semua gagal, dipakai fallback
  berbasis aturan: atasan, bawahan, dan alas kaki yang paling cocok dengan kata
  kunci prompt, gender konsisten, bukan produk anak (kecuali diminta), dan
  masuk budget.
- Jenis produk mengikuti prompt. "carikan baju formal" hanya memilih atasan;
  "celana dan sepatu" memilih bawahan + alas kaki; "topi" hanya topi. Jika
  prompt tidak menyebut jenis produk, atau memakai kata seperti "outfit" /
  "setelan", sistem memilih atasan + bawahan + alas kaki. Topi dan kacamata
  hanya dipilih jika disebut (atau "aksesoris").
- Model rembg dimuat saat startup, dan hasil hapus-background produk di-cache
  di memori, sehingga komposit gambar hanya butuh puluhan milidetik.
- Endpoint rekomendasi berjalan di thread pool, jadi request pelanggan lain
  tidak antre di belakang panggilan Gemini.

## Jaminan Anti-Halusinasi
- AI hanya menerima katalog dari DB dan hanya boleh memilih `id` yang ada; ID
  karangan otomatis dibuang.
- Harga dan `total_price` selalu dihitung dari data DB, bukan dari output AI.
- `within_budget` dihitung ulang di server.

## Pemeriksaan Manual Dasar
1. Jalankan server, buka `/scalar`.
2. Buat beberapa item lewat `POST /api/v1/items`.
3. Panggil `POST /api/v1/recommendations` dengan foto + budget.
4. Pastikan item yang dikembalikan ada di DB, total ≤ budget, dan ada alasan.
   Jika kuota image editing habis, cek `image_editing_error` terisi dan
   rekomendasi tetap muncul.
```
