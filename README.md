# PDF Resizer & Label Fitter (100×100 mm)

Web app Python (Flask) untuk mengubah ukuran halaman PDF apa pun (mis. label
pengiriman Shopee ukuran ~105×148 mm dengan banyak ruang kosong) menjadi
ukuran target seperti **100 × 100 mm**.

Mode default sekarang **Fit Lebar**: konten diperbesar sampai **penuh
kiri–kanan**, rasio aspek tetap dijaga (tidak gepeng), dan sisa ruang di atas
dan bawah dibagi rata sehingga label **rata tengah secara vertikal**.

## Yang diperbaiki dari versi sebelumnya

1. **Auto-crop sekarang benar-benar bekerja.**
   Label Shopee punya satu **kotak putih sebesar satu halaman penuh** di
   lapisan paling bawah. Versi lama ikut menghitung kotak itu sebagai
   "konten", sehingga bounding box = seluruh halaman dan auto-crop tidak
   membuang apa-apa. Akibatnya konten harus di-*stretch* habis-habisan dan
   hasilnya gepeng / tidak pas.
   Sekarang deteksi konten mengabaikan:
   - karakter spasi / glyph kosong,
   - shape yang tidak di-*fill* maupun di-*stroke* (tidak terlihat),
   - shape ber-*fill* putih tanpa garis,
   - shape yang menutupi ≥ 92% luas halaman (background halaman).

2. **Mode baru `fit_width`** (default) — skala seragam agar konten pas penuh
   selebar kanvas, lalu dipusatkan vertikal. Ini yang Anda minta.
   Ditambah `fit_height` (penuh atas–bawah, tengah horizontal).

3. **Selalu rata tengah** pada sumbu yang tersisa, untuk semua mode.

4. **Anti-terpotong** (`no_overflow`, default aktif): kalau hasil fit-lebar
   ternyata lebih tinggi dari kanvas, skala otomatis diturunkan agar tidak ada
   bagian label yang terpotong.

5. Menghormati `/Rotate` halaman, mediabox yang origin-nya bukan (0,0), dan
   menyetel `cropbox` hasil = ukuran target.

## Struktur Proyek

```
pdf-resizer/
├── api/
│   └── index.py         # Flask app + seluruh logika resize (SATU FILE)
├── requirements.txt
├── vercel.json          # Konfigurasi deploy Vercel (Python serverless)
├── .gitignore
└── README.md
```

> **Kenapa satu file saja?** Di beberapa setup Vercel, file `.py` kedua di
> dalam `api/` tidak ikut ter-bundle oleh `@vercel/python`, sehingga
> `import` gagal dan semua request berakhir `500
> FUNCTION_INVOCATION_FAILED`. Semua logika digabung di `api/index.py`.

## Menjalankan Secara Lokal

Butuh Python 3.9+.

```bash
cd pdf-resizer
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
python3 api/index.py
```

Buka http://127.0.0.1:5000, upload PDF, pilih mode, lalu **Convert & Download PDF**.

## Deploy ke Vercel

### Opsi A — via GitHub (disarankan)

```bash
cd pdf-resizer
git init
git add .
git commit -m "PDF resizer: fit-width + vertical center"
git branch -M main
git remote add origin https://github.com/USERNAME/NAMA-REPO.git
git push -u origin main
```

Lalu buka https://vercel.com/new → **Import Git Repository** → pilih repo →
**Deploy**. Vercel otomatis membaca `vercel.json`; Build Command / Output
Directory biarkan default.

### Opsi B — via Vercel CLI

```bash
npm install -g vercel
cd pdf-resizer
vercel login
vercel --prod
```

## Endpoint

- `GET /` — Halaman UI upload.
- `POST /api/convert` — `multipart/form-data`:
  | field | isi | default |
  |---|---|---|
  | `file` | file PDF | wajib |
  | `width_mm`, `height_mm` | ukuran target (mm) | 100, 100 |
  | `margin_mm` | margin semua sisi (mm) | 0 |
  | `mode` | `fit_width` \| `fit_height` \| `fit` \| `stretch` | `fit_width` |
  | `auto_crop` | `1` / `0` | `1` |
  | `no_overflow` | `1` / `0` | `1` |

  Response: file PDF hasil (`application/pdf`).
- `GET /api/health` — `{"status": "ok"}`.

Contoh cURL:

```bash
curl -X POST https://NAMA-APP.vercel.app/api/convert \
  -F "file=@label.pdf" \
  -F "width_mm=100" -F "height_mm=100" \
  -F "mode=fit_width" -F "auto_crop=1" \
  -o label_100x100.pdf
```

## Catatan Teknis

- Deteksi area konten memakai **pdfplumber** (pure Python) — bukan PyMuPDF —
  supaya bundle Vercel jauh lebih kecil dan tidak bergantung binary native.
- Transformasi ukuran memakai **pypdf**
  (`Transformation().scale(...).translate(...)`), hasilnya di-`merge_page` ke
  halaman baru berukuran target.
- Batas upload 15 MB (`MAX_CONTENT_LENGTH` di `api/index.py`). Body request
  fungsi serverless Vercel paket gratis ± 4.5 MB — gunakan file lebih kecil
  bila perlu.

## Troubleshooting

**500 FUNCTION_INVOCATION_FAILED setelah deploy:**
1. Vercel Dashboard → project → **Logs** / **Functions**, ulangi request yang
   gagal; error Python asli muncul di sana.
2. Jangan menambah file `.py` lain di dalam `api/` tanpa mengatur
   `config.includeFiles` di `vercel.json`.
3. Pastikan `requirements.txt` ter-commit dan berada di **root** project
   (sejajar `vercel.json`).
4. Coba redeploy bersih (hapus project di Vercel, import ulang repo).

**Hasil masih ada ruang kosong di kiri/kanan:** artinya konten PDF asli memang
lebih tinggi daripada lebar setelah di-crop, dan opsi "jangan potong" menahan
skala agar tidak terpotong. Matikan centang *"Jangan potong konten bila
melebihi kanvas"* jika Anda memang ingin penuh selebar kanvas walau bagian
atas/bawah terpotong.

## Lisensi

Bebas digunakan dan dimodifikasi.
