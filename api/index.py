import os
import traceback
from io import BytesIO

from flask import Flask, request, send_file, jsonify, Response
import pdfplumber
from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import RectangleObject

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Core PDF resize logic (kept in this single file so Vercel's Python builder
# always bundles it correctly alongside the Flask app — no sibling-module
# import issues). Uses pdfplumber (pure Python, for content-bbox detection)
# + pypdf (for the actual scale/translate transform).
# ---------------------------------------------------------------------------

MM_TO_PT = 72.0 / 25.4  # 1 mm in PDF points

VALID_MODES = ("fit_width", "fit_height", "fit", "stretch")


def mm_to_pt(mm: float) -> float:
    return mm * MM_TO_PT


def _is_white(color):
    """True if a pdfplumber color tuple/scalar is (near) white."""
    if color is None:
        return False
    if isinstance(color, (int, float)):
        return float(color) >= 0.95
    try:
        vals = [float(c) for c in color]
    except (TypeError, ValueError):
        return False
    if not vals:
        return False
    if len(vals) == 4:  # CMYK: white = 0,0,0,0
        return all(v <= 0.05 for v in vals)
    return all(v >= 0.95 for v in vals)


def _shape_is_visible(obj, page_area):
    """
    Filter out shapes that do not actually paint anything the eye can see:
    - neither filled nor stroked
    - filled with white (and not stroked) -> background blocks
    - covering (almost) the whole page -> page background
    These are the usual reason auto-crop "does nothing" on marketplace labels:
    the PDF has one full-page white rectangle behind everything.
    """
    filled = bool(obj.get("fill"))
    stroked = bool(obj.get("stroke"))

    if not filled and not stroked:
        return False

    if filled and not stroked and _is_white(obj.get("non_stroking_color")):
        return False
    if stroked and not filled and _is_white(obj.get("stroking_color")):
        return False

    w = abs(float(obj.get("x1", 0)) - float(obj.get("x0", 0)))
    h = abs(float(obj.get("bottom", 0)) - float(obj.get("top", 0)))
    if page_area > 0 and (w * h) >= 0.92 * page_area:
        return False

    return True


def _detect_content_bbox(plumber_page, page_height_pt):
    """
    Union bounding box (in PDF space, origin bottom-left) of everything that is
    actually visible on the page: non-blank text characters, visible
    rects/lines/curves and images.
    Returns None if nothing usable was found.
    """
    page_area = float(plumber_page.width) * float(plumber_page.height)
    x0s, x1s, tops, bottoms = [], [], [], []

    def add(obj):
        x0s.append(float(obj["x0"]))
        x1s.append(float(obj["x1"]))
        tops.append(float(obj["top"]))
        bottoms.append(float(obj["bottom"]))

    for ch in plumber_page.chars:
        if str(ch.get("text", "")).strip() == "":
            continue  # spaces / blank glyphs must not inflate the bbox
        add(ch)

    for collection in (plumber_page.rects, plumber_page.lines, plumber_page.curves):
        for obj in collection:
            if _shape_is_visible(obj, page_area):
                add(obj)

    for img in plumber_page.images:
        add(img)

    if not x0s:
        return None

    x0, x1 = min(x0s), max(x1s)
    top, bottom = min(tops), max(bottoms)

    # pdfplumber uses a top-left origin; PDF space uses bottom-left.
    y0 = page_height_pt - bottom
    y1 = page_height_pt - top

    return (x0, y0, x1, y1)


def resize_pdf(
    input_bytes: bytes,
    target_width_mm: float = 100.0,
    target_height_mm: float = 100.0,
    margin_mm: float = 0.0,
    mode: str = "fit_width",
    auto_crop: bool = True,
    no_overflow: bool = True,
) -> bytes:
    """
    Rebuild every page of the input PDF onto a new page of
    target_width_mm x target_height_mm, with margin_mm of blank margin.

    mode:
      "fit_width"  -> uniform scale so the content fills the full width
                      (left-right edge to edge) and is centered vertically.
      "fit_height" -> uniform scale to fill the full height, centered
                      horizontally.
      "fit"        -> uniform scale that fits inside both axes, centered.
      "stretch"    -> non-uniform scale, fills the canvas exactly (distorts).

    auto_crop: crop to the visible-content bounding box first, so blank space
               on the source page is discarded before scaling.
    no_overflow: with fit_width/fit_height, shrink further if the other axis
               would overflow the page (prevents the label being cut off).
    """
    if target_width_mm <= 0 or target_height_mm <= 0:
        raise ValueError("Ukuran target harus lebih besar dari 0.")
    if margin_mm < 0:
        raise ValueError("Margin tidak boleh negatif.")
    if mode not in VALID_MODES:
        raise ValueError("Mode tidak dikenal.")

    target_w = mm_to_pt(target_width_mm)
    target_h = mm_to_pt(target_height_mm)
    margin = mm_to_pt(margin_mm)

    avail_w = target_w - 2 * margin
    avail_h = target_h - 2 * margin
    if avail_w <= 0 or avail_h <= 0:
        raise ValueError("Margin terlalu besar untuk ukuran target.")

    reader = PdfReader(BytesIO(input_bytes))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            raise ValueError("PDF terkunci password dan tidak bisa diproses.")

    writer = PdfWriter()
    plumber_pdf = pdfplumber.open(BytesIO(input_bytes)) if auto_crop else None

    try:
        for idx, page in enumerate(reader.pages):
            # Normalise /Rotate so the geometry below matches what is seen.
            if page.get("/Rotate"):
                try:
                    page.transfer_rotation_to_content()
                except Exception:
                    pass

            box = page.mediabox
            page_x0, page_y0 = float(box.left), float(box.bottom)
            orig_w = float(box.width)
            orig_h = float(box.height)

            bbox = None
            if plumber_pdf is not None and idx < len(plumber_pdf.pages):
                try:
                    bbox = _detect_content_bbox(plumber_pdf.pages[idx], orig_h)
                except Exception:
                    bbox = None

            if bbox is None:
                bx0, by0, bx1, by1 = page_x0, page_y0, page_x0 + orig_w, page_y0 + orig_h
            else:
                bx0, by0, bx1, by1 = bbox
                # pdfplumber coordinates are relative to the mediabox origin.
                bx0 += page_x0
                bx1 += page_x0
                by0 += page_y0
                by1 += page_y0
                bx0 = max(page_x0, bx0)
                by0 = max(page_y0, by0)
                bx1 = min(page_x0 + orig_w, bx1)
                by1 = min(page_y0 + orig_h, by1)

            clip_w = bx1 - bx0
            clip_h = by1 - by0
            if clip_w <= 1 or clip_h <= 1:
                bx0, by0 = page_x0, page_y0
                bx1, by1 = page_x0 + orig_w, page_y0 + orig_h
                clip_w, clip_h = orig_w, orig_h

            if mode == "stretch":
                sx = avail_w / clip_w
                sy = avail_h / clip_h
            elif mode == "fit_width":
                s = avail_w / clip_w
                if no_overflow and clip_h * s > avail_h:
                    s = avail_h / clip_h
                sx = sy = s
            elif mode == "fit_height":
                s = avail_h / clip_h
                if no_overflow and clip_w * s > avail_w:
                    s = avail_w / clip_w
                sx = sy = s
            else:  # "fit"
                sx = sy = min(avail_w / clip_w, avail_h / clip_h)

            new_w = clip_w * sx
            new_h = clip_h * sy

            # Always centre whatever is left over on each axis.
            tx = margin + (avail_w - new_w) / 2 - bx0 * sx
            ty = margin + (avail_h - new_h) / 2 - by0 * sy

            page.add_transformation(Transformation().scale(sx, sy).translate(tx, ty))

            new_page = writer.add_blank_page(width=target_w, height=target_h)
            new_page.mediabox = RectangleObject((0, 0, target_w, target_h))
            new_page.cropbox = RectangleObject((0, 0, target_w, target_h))
            new_page.merge_page(page)
    finally:
        if plumber_pdf is not None:
            plumber_pdf.close()

    out = BytesIO()
    writer.write(out)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

MAX_CONTENT_LENGTH = 15 * 1024 * 1024  # 15 MB safety cap
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH


INDEX_HTML = """<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>PDF Resizer &mdash; Ubah Ukuran Label PDF (100x100 mm)</title>
<style>
  :root {
    --bg: #0f1220;
    --card: #171b2e;
    --border: #2a3050;
    --text: #eef0fb;
    --muted: #9aa1c4;
    --accent: #ff6a3d;
    --accent-dark: #e2531f;
    --ok: #3ddc97;
    --err: #ff5c7c;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: radial-gradient(1200px 600px at 20% -10%, #232a4d 0%, var(--bg) 60%);
    color: var(--text);
    min-height: 100vh;
    padding: 32px 16px 64px;
  }
  .wrap { max-width: 760px; margin: 0 auto; }
  header { text-align: center; margin-bottom: 28px; }
  header h1 { font-size: 1.7rem; margin: 0 0 8px; letter-spacing: -0.02em; }
  header p { color: var(--muted); margin: 0; font-size: 0.95rem; }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 16px;
    padding: 24px;
    margin-bottom: 20px;
    box-shadow: 0 10px 30px rgba(0,0,0,0.25);
  }
  .dropzone {
    border: 2px dashed var(--border);
    border-radius: 12px;
    padding: 28px 16px;
    text-align: center;
    cursor: pointer;
    transition: border-color .15s, background .15s;
    color: var(--muted);
  }
  .dropzone.dragover, .dropzone:hover { border-color: var(--accent); background: rgba(255,106,61,0.06); }
  .dropzone strong { color: var(--text); }
  #fileInfo { margin-top: 10px; font-size: 0.9rem; color: var(--ok); display: none; }
  input[type=file] { display: none; }

  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 20px; }
  .full { grid-column: 1 / -1; }
  label { display: block; font-size: 0.82rem; color: var(--muted); margin-bottom: 6px; }
  input[type=number], select {
    width: 100%;
    padding: 10px 12px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background-color: #0f1329;
    color: var(--text);
    font-size: 0.95rem;
  }
  input[type=number]:focus, select:focus { outline: none; border-color: var(--accent); }

  .mode-toggle { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }
  .mode-toggle button {
    padding: 10px 8px;
    border-radius: 8px;
    border: 1px solid var(--border);
    background: #0f1329;
    color: var(--muted);
    cursor: pointer;
    font-size: 0.86rem;
    transition: all .15s;
    text-align: left;
    line-height: 1.3;
  }
  .mode-toggle button small { display: block; font-size: 0.72rem; opacity: .75; margin-top: 3px; }
  .mode-toggle button:hover { border-color: var(--accent); }
  .mode-toggle button.active {
    border-color: var(--accent);
    background: rgba(255,106,61,0.12);
    color: var(--text);
  }

  .presets { display: flex; flex-wrap: wrap; gap: 8px; }
  .presets button {
    padding: 7px 12px;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: #0f1329;
    color: var(--muted);
    font-size: 0.8rem;
    cursor: pointer;
  }
  .presets button:hover { border-color: var(--accent); color: var(--text); }

  .checkbox-row { display: flex; align-items: center; gap: 9px; font-size: 0.86rem; color: var(--muted); }
  .checkbox-row input { width: 16px; height: 16px; accent-color: var(--accent); }

  .submit-btn {
    width: 100%;
    margin-top: 22px;
    padding: 14px;
    border: none;
    border-radius: 10px;
    background: var(--accent);
    color: #fff;
    font-size: 1rem;
    font-weight: 600;
    cursor: pointer;
    transition: background .15s;
  }
  .submit-btn:hover { background: var(--accent-dark); }
  .submit-btn:disabled { opacity: .6; cursor: not-allowed; }

  #status { margin-top: 14px; font-size: 0.88rem; min-height: 20px; text-align: center; color: var(--muted); }
  #status.ok { color: var(--ok); }
  #status.err { color: var(--err); }

  footer { text-align: center; color: var(--muted); font-size: 0.78rem; margin-top: 28px; }
  @media (max-width: 560px) { .grid { grid-template-columns: 1fr; } .mode-toggle { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>&#128208; PDF Resizer &amp; Label Fitter</h1>
    <p>Ubah ukuran halaman PDF (mis. label pengiriman) ke 100&times;100&nbsp;mm. Mode default: konten <strong>penuh kiri-kanan</strong>, <strong>rata tengah atas-bawah</strong>, rasio tetap terjaga (tidak gepeng).</p>
  </header>

  <form id="form" class="card">
    <div class="dropzone" id="dropzone">
      <div>&#128196; Klik atau seret file <strong>PDF</strong> ke sini</div>
      <div id="fileInfo"></div>
      <input type="file" id="fileInput" name="file" accept="application/pdf" />
    </div>

    <div class="grid">
      <div>
        <label for="width">Lebar target (mm)</label>
        <input type="number" id="width" step="0.1" min="1" value="100" />
      </div>
      <div>
        <label for="height">Tinggi target (mm)</label>
        <input type="number" id="height" step="0.1" min="1" value="100" />
      </div>
      <div class="full">
        <div class="presets">
          <button type="button" data-w="100" data-h="100">100&times;100 mm</button>
          <button type="button" data-w="100" data-h="150">100&times;150 mm</button>
          <button type="button" data-w="10" data-h="10">10&times;10 cm</button>
          <button type="button" data-w="4" data-h="6" data-unit="in">4&times;6 in</button>
          <button type="button" data-w="210" data-h="297">A4</button>
        </div>
      </div>

      <div class="full">
        <label>Mode penyesuaian</label>
        <div class="mode-toggle">
          <button type="button" data-mode="fit_width" class="active">Fit Lebar &#11088;<small>Penuh kiri-kanan, rata tengah atas-bawah</small></button>
          <button type="button" data-mode="fit">Fit Proporsional<small>Muat di dalam kanvas, rata tengah</small></button>
          <button type="button" data-mode="fit_height">Fit Tinggi<small>Penuh atas-bawah, rata tengah kiri-kanan</small></button>
          <button type="button" data-mode="stretch">Stretch<small>Isi penuh, rasio berubah (gepeng)</small></button>
        </div>
      </div>

      <div>
        <label for="margin">Margin (mm)</label>
        <input type="number" id="margin" step="0.1" min="0" value="0" />
      </div>
      <div>
        <label>Opsi</label>
        <div class="checkbox-row" style="margin-bottom:8px;">
          <input type="checkbox" id="autocrop" checked />
          <span>Auto-crop area kosong sebelum resize</span>
        </div>
        <div class="checkbox-row">
          <input type="checkbox" id="nooverflow" checked />
          <span>Jangan potong konten bila melebihi kanvas</span>
        </div>
      </div>
    </div>

    <button type="submit" class="submit-btn" id="submitBtn">Convert &amp; Download PDF</button>
    <div id="status"></div>
  </form>

  <footer>
    Diproses di server (Python + pypdf + pdfplumber) &middot; File tidak disimpan permanen.
  </footer>
</div>

<script>
const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('fileInput');
const fileInfo = document.getElementById('fileInfo');
const form = document.getElementById('form');
const statusEl = document.getElementById('status');
const submitBtn = document.getElementById('submitBtn');
let mode = 'fit_width';

dropzone.addEventListener('click', () => fileInput.click());
dropzone.addEventListener('dragover', (e) => { e.preventDefault(); dropzone.classList.add('dragover'); });
dropzone.addEventListener('dragleave', () => dropzone.classList.remove('dragover'));
dropzone.addEventListener('drop', (e) => {
  e.preventDefault();
  dropzone.classList.remove('dragover');
  if (e.dataTransfer.files.length) {
    fileInput.files = e.dataTransfer.files;
    updateFileInfo();
  }
});
fileInput.addEventListener('change', updateFileInfo);

function updateFileInfo() {
  if (fileInput.files.length) {
    const f = fileInput.files[0];
    fileInfo.style.display = 'block';
    fileInfo.textContent = '\\u2713 ' + f.name + ' (' + (f.size/1024).toFixed(0) + ' KB)';
  }
}

document.querySelectorAll('.presets button').forEach(btn => {
  btn.addEventListener('click', () => {
    let w = parseFloat(btn.dataset.w);
    let h = parseFloat(btn.dataset.h);
    if (btn.dataset.unit === 'in') { w *= 25.4; h *= 25.4; }
    if (btn.dataset.w === '10' && btn.dataset.h === '10') { w = 100; h = 100; }
    document.getElementById('width').value = Math.round(w * 10) / 10;
    document.getElementById('height').value = Math.round(h * 10) / 10;
  });
});

document.querySelectorAll('.mode-toggle button').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.mode-toggle button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    mode = btn.dataset.mode;
  });
});

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  statusEl.className = '';
  statusEl.textContent = '';

  if (!fileInput.files.length) {
    statusEl.className = 'err';
    statusEl.textContent = 'Pilih file PDF terlebih dahulu.';
    return;
  }

  const fd = new FormData();
  fd.append('file', fileInput.files[0]);
  fd.append('width_mm', document.getElementById('width').value);
  fd.append('height_mm', document.getElementById('height').value);
  fd.append('margin_mm', document.getElementById('margin').value);
  fd.append('mode', mode);
  fd.append('auto_crop', document.getElementById('autocrop').checked ? '1' : '0');
  fd.append('no_overflow', document.getElementById('nooverflow').checked ? '1' : '0');

  submitBtn.disabled = true;
  submitBtn.textContent = 'Memproses...';
  statusEl.textContent = 'Mengunggah dan memproses PDF...';

  try {
    const res = await fetch('/api/convert', { method: 'POST', body: fd });
    if (!res.ok) {
      const err = await res.json().catch(() => ({error: 'Terjadi kesalahan.'}));
      throw new Error(err.error || 'Gagal memproses PDF.');
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    const origName = fileInput.files[0].name.replace(/\\.pdf$/i, '');
    a.href = url;
    a.download = origName + '_resized.pdf';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    statusEl.className = 'ok';
    statusEl.textContent = '\\u2713 Selesai! PDF hasil sudah terunduh.';
  } catch (err) {
    statusEl.className = 'err';
    statusEl.textContent = err.message;
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Convert & Download PDF';
  }
});
</script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
@app.route("/api", methods=["GET"])
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/api/convert", methods=["POST"])
def convert():
    try:
        if "file" not in request.files:
            return jsonify({"error": "Tidak ada file yang diunggah."}), 400

        f = request.files["file"]
        if f.filename == "":
            return jsonify({"error": "Nama file kosong."}), 400
        if not f.filename.lower().endswith(".pdf"):
            return jsonify({"error": "File harus berformat .pdf"}), 400

        data = f.read()
        if not data:
            return jsonify({"error": "File kosong atau gagal dibaca."}), 400

        def get_float(name, default):
            try:
                return float(request.form.get(name, default))
            except (TypeError, ValueError):
                return float(default)

        result = resize_pdf(
            data,
            target_width_mm=get_float("width_mm", 100),
            target_height_mm=get_float("height_mm", 100),
            margin_mm=get_float("margin_mm", 0),
            mode=request.form.get("mode", "fit_width"),
            auto_crop=request.form.get("auto_crop", "1") == "1",
            no_overflow=request.form.get("no_overflow", "1") == "1",
        )

        base_name = os.path.splitext(f.filename)[0]
        return send_file(
            BytesIO(result),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"{base_name}_resized.pdf",
        )

    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Terjadi kesalahan saat memproses PDF."}), 500


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# Local dev entrypoint: `python3 api/index.py`
if __name__ == "__main__":
    app.run(debug=True, port=5000)
