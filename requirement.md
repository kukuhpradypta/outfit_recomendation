# Project Brief — AI Outfit Recommendation Backend

## Tech Stack
- Python 3.12
- FastAPI
- PostgreSQL
- SQLAlchemy
- Gemini API
- Scalar API Reference sebagai GUI untuk dokumentasi dan mencoba endpoint

## Tujuan
Buat backend API untuk merekomendasikan pakaian berdasarkan katalog produk toko, foto pengguna atau mannequin, estimasi budget, dan prompt tambahan opsional.

AI harus memilih produk yang benar-benar tersedia di database. Hasil rekomendasi mencakup produk yang dipilih, alasan pemilihan, dan visualisasi pengguna yang mengenakan pakaian rekomendasi tersebut.

## Database

Gunakan PostgreSQL dengan satu tabel utama `item`.

Kolom yang disarankan:
- `id`: primary key
- `name`: nama produk
- `category`: kategori pakaian
- `color_tone`: warna atau tone warna
- `price`: harga produk
- `image_path`: path relatif gambar di folder `public/`
- `description`: deskripsi produk, opsional
- `created_at`: waktu pembuatan data

Gambar disimpan sebagai file di folder `public/`, sedangkan database menyimpan path gambar relatifnya.

## API Utama

### 1. `POST /api/v1/items`
Membuat item produk baru dengan data produk dan upload gambar.

- Validasi input dan format gambar.
- Simpan gambar ke folder `public/`.
- Simpan metadata dan `image_path` ke PostgreSQL.
- Kembalikan data item yang berhasil dibuat.

### 2. `POST /api/v1/recommendations`
Menerima:
- Foto pengguna atau mannequin.
- Estimasi budget.
- Prompt tambahan opsional, misalnya gaya formal, streetwear, minimalis, atau warna tertentu.

Alur:
1. Ambil katalog produk dari PostgreSQL.
2. Pilih produk yang sesuai dengan budget dan preferensi.
3. Gunakan Gemini Flash untuk menganalisis kebutuhan dan menghasilkan rekomendasi beserta alasan pemilihannya.
4. Gunakan model image generation/editing Gemini yang sesuai, misalnya Nano Banana, untuk mengganti pakaian pada foto input menggunakan produk yang dipilih.
5. Kembalikan daftar produk, harga, total estimasi, alasan rekomendasi, dan hasil gambar jika berhasil.

Pastikan AI tidak mengarang produk atau harga. Produk rekomendasi wajib berasal dari tabel `item`, dan total harga harus sesuai dengan data database serta budget pengguna.

Pisahkan proses pemilihan produk, penjelasan, dan image editing agar mudah dikembangkan.

## Integrasi Gemini
- Gunakan Gemini Flash untuk analisis kebutuhan dan penjelasan rekomendasi.
- Gunakan model Nano Banana yang tersedia dan mendukung image editing untuk visualisasi outfit.
- Simpan API key di environment variable.
- Jangan mengasumsikan nama model tertentu selalu tersedia; gunakan model ID yang dikonfigurasi dan sesuai dengan API yang aktif.
- Jika image editing gagal, tetap kembalikan rekomendasi produk beserta alasannya.

## GUI dan Dokumentasi
Gunakan Scalar API Reference yang terintegrasi dengan FastAPI agar endpoint dapat dilihat dan dicoba melalui browser.

Tidak perlu membuat frontend pelanggan, dashboard admin, atau aplikasi Streamlit.

## Struktur Minimal
- `main.py`
- `app/routers/`
- `app/schemas/`
- `app/services/`
- `app/repositories/`
- `app/database.py`
- `app/config.py`
- `public/images/items/`
- `requirements.txt`
- `.env.example`
- `README.md`

## Batasan
- Gunakan Python 3.12.
- PostgreSQL menjadi sumber data utama produk.
- Jangan membuat tabel tambahan kecuali benar-benar diperlukan.
- Tidak perlu automated test suite atau folder `tests/`.
- Tidak perlu fitur login, dashboard, virtual try-on terpisah, atau sistem analitik.
- Tetap jalankan pemeriksaan manual dasar untuk memastikan API dapat digunakan.
- Dokumentasikan instalasi, konfigurasi database, Gemini API, dan cara menjalankan backend.

Fokus pada implementasi yang sederhana, berfungsi, dan mudah diintegrasikan dengan frontend sebagai aplikasi sederhana.