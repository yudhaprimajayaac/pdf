# PDF Resizer & Stretch (100x100 Label Converter)

Web app Python (Flask) untuk mengubah ukuran halaman PDF apa pun (misalnya
label pengiriman dengan ukuran custom / A6) menjadi ukuran target tertentu
seperti **100 x 100 mm**, dengan:

- **Stretch** — konten di-*stretch* (non-uniform scale) supaya mengisi penuh
  kanvas target persis, tanpa sisa area kosong.
- **Fit** — rasio aspek konten dipertahankan, dipusatkan, dan sisa ruang jadi
  margin otomatis.
- **Auto-crop** — mendeteksi bounding box konten asli (teks, gambar/barcode,
  garis kotak) sehingga area kosong pada PDF asli dibuang dulu sebelum
  di-resize — inilah yang membuat hasil "penuh" seperti contoh 100x100 Anda,
  bukan sekadar PDF yang diperkecil dengan banyak ruang kosong.
- **Margin manual** — tambahkan margin (mm) di semua sisi jika diperlukan.
- Preset ukuran cepat (100x100mm, 100x150mm, 10x10cm, 4x6in, A4) dan input
  ukuran custom.

Contoh kasus nyata yang jadi acuan: PDF label Shopee ukuran ~105x148mm
(banyak ruang kosong di bawah) → dikonversi jadi PDF label rapi 100x100mm
persis seperti template kurir JTR.

## Struktur Proyek

```
pdf-resizer/
├── api/
│   └── index.py         # Flask app + logika resize/stretch/fit/margin (SATU FILE)
├── requirements.txt
├── vercel.json          # Konfigurasi deploy Vercel (Python serverless)
├── .gitignore
└── README.md
```

> **Kenapa satu file saja?** Awalnya logika resize dipisah ke
> `api/pdf_resizer.py` lalu di-`import` oleh `api/index.py`. Di beberapa
> setup Vercel, file kedua ini tidak ikut ter-bundle oleh builder
> `@vercel/python`, sehingga `import pdf_resizer` gagal dan **semua**
> request (termasuk halaman utama) berakhir `500
> FUNCTION_INVOCATION_FAILED`. Semua logika sekarang digabung ke satu file
> `api/index.py` supaya tidak ada risiko impor antar-file yang gagal
> ter-bundle.

## Menjalankan Secara Lokal

Butuh Python 3.9+.

```bash
cd pdf-resizer
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 api/index.py
```

Buka http://127.0.0.1:5000 di browser, upload PDF, atur ukuran target /
margin / mode, lalu klik **Convert & Download PDF**.

## Deploy ke Vercel

### Opsi A — via GitHub (disarankan)

1. Buat repo baru di GitHub, lalu push isi folder ini:
   ```bash
   cd pdf-resizer
   git init
   git add .
   git commit -m "Initial commit: PDF resizer app"
   git branch -M main
   git remote add origin https://github.com/USERNAME/NAMA-REPO.git
   git push -u origin main
   ```
2. Buka https://vercel.com/new, pilih **Import Git Repository**, pilih repo
   tadi.
3. Vercel akan otomatis mendeteksi `vercel.json` dan menjalankan
   `@vercel/python`. Tidak perlu mengubah Build Command / Output Directory —
   biarkan default.
4. Klik **Deploy**. Setelah selesai, app bisa diakses di URL `*.vercel.app`
   yang diberikan.

### Opsi B — via Vercel CLI (tanpa GitHub)

```bash
npm install -g vercel
cd pdf-resizer
vercel login
vercel --prod
```

## Cara Kerja Endpoint

- `GET /` — Halaman UI upload.
- `POST /api/convert` — Terima `multipart/form-data`:
  - `file`: file PDF
  - `width_mm`, `height_mm`: ukuran target (mm)
  - `margin_mm`: margin (mm), default 0
  - `mode`: `stretch` atau `fit`
  - `auto_crop`: `1` atau `0`
  - Response: file PDF hasil (`application/pdf`) siap diunduh.
- `GET /api/health` — Health check sederhana (`{"status": "ok"}`).

## Catatan Teknis

- Deteksi area konten (untuk auto-crop) memakai **pdfplumber** (pure
  Python, membaca posisi teks/garis/gambar) — bukan PyMuPDF — supaya
  package yang di-deploy ke Vercel jauh lebih kecil (~25 MB total vs ~60+ MB
  jika pakai PyMuPDF) dan tidak bergantung pada binary native yang kadang
  bermasalah di lingkungan serverless.
- Transformasi ukuran (scale + translate) dilakukan dengan **pypdf**
  (`Transformation().scale(...).translate(...)`), lalu hasilnya digabung
  (`merge_page`) ke halaman baru berukuran target.
- Mode *stretch* menskalakan sumbu X dan Y secara independen agar pas persis
  dengan area target (dikurangi margin).
- Mode *fit* menskalakan secara seragam (aspect ratio terjaga) dan
  memusatkan hasil di tengah area target.
- Ukuran file dibatasi 15 MB per upload (bisa diubah di `MAX_CONTENT_LENGTH`
  pada `api/index.py`) — sesuaikan dengan limit body Vercel (default fungsi
  serverless Vercel ± 4.5 MB untuk request body pada paket gratis; naikkan
  paket atau gunakan file lebih kecil bila perlu).

## Troubleshooting

**Muncul "500 FUNCTION_INVOCATION_FAILED" setelah deploy:**
1. Buka **Vercel Dashboard → nama project → tab "Logs"** (atau "Functions"),
   lalu ulangi request yang gagal — pesan error Python yang sebenarnya akan
   muncul di sana (mis. `ModuleNotFoundError`, dsb). Ini paling cepat untuk
   tahu akar masalahnya.
2. Pastikan struktur project **persis** seperti di atas — jangan menambah
   file `.py` lain di dalam folder `api/` kecuali Anda juga mengatur
   `vercel.json` (`config.includeFiles`) untuk membundelnya.
3. Pastikan `requirements.txt` ikut ter-commit ke Git dan berada di **root**
   folder project (sejajar dengan `vercel.json`), bukan di dalam `api/`.
4. Coba redeploy dari awal: hapus project di Vercel, lalu import ulang repo
   GitHub-nya (kadang cache build lama menyebabkan masalah).
5. Jika masih gagal, jalankan dulu secara lokal (`python3 api/index.py`) —
   jika lokal berjalan normal tapi Vercel tetap 500, hampir pasti
   penyebabnya ada di log function Vercel (langkah 1), bukan di kode.

## Lisensi

Bebas digunakan dan dimodifikasi sesuai kebutuhan Anda.
