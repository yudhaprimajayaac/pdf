import os
import re
import traceback
from datetime import datetime, timedelta, timezone
from io import BytesIO

from flask import Flask, request, send_file, jsonify, Response
import pdfplumber
from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.generic import RectangleObject
from reportlab.lib.pagesizes import A5
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.pdfbase.pdfmetrics import stringWidth

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
# Baca label Shopee -> data surat jalan  (PEMBELI / ALAMAT / PIC / VIA / TGL / UNIT-QTY)
# Dibuat berdasarkan label J&T/Shopee (kolom Penerima | Pengirim, teks resi diputar
# 90 derajat). Semua posisi dihitung relatif, jadi label asli (105x148) maupun
# hasil resize 100x100 terbaca sama.
# ---------------------------------------------------------------------------

DEFAULT_SUFFIX = "Bubble Wrap"
WIB = timezone(timedelta(hours=7))

BULAN = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
         "Agustus", "September", "Oktober", "November", "Desember"]
_BULAN_MAP = {}
for _i, _n in enumerate(BULAN, 1):
    _BULAN_MAP[_n.lower()] = _i
    _BULAN_MAP[_n[:3].lower()] = _i
_BULAN_MAP.update({"agt": 8, "agu": 8, "ags": 8, "okt": 10, "des": 12,
                   "may": 5, "aug": 8, "oct": 10, "dec": 12})

_STOP_RE = re.compile(
    r"^\s*(?:pengirim|dari|sender|from|berat|weight|batas\s*kirim|kirim\s*sebelum|ship\s*by|"
    r"no\.?\s*pesanan|nomor\s*pesanan|order\s*(?:id|no)|nama\s*produk|qty|cod\b|cashless|"
    r"resi|no\.?\s*resi|tracking)\b", re.I)
_RECIPIENT_RE = re.compile(r"^\s*(?:penerima|kepada|deliver\s*to|ship\s*to)\s*[:\-]?\s*(.*)$", re.I)
_PHONE_TAIL_RE = re.compile(
    r"[\s(]*(?:\+?\d{1,3}\)\s*|\(\+?\d{1,3}\)\s*)?[\d*][\d*\s\-]{4,}\)?\s*$")
_PHONE_LINE_RE = re.compile(r"^\s*\(?\+?[\d*][\d*\s\-()]{6,}\s*$")


def _clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip(" \t:-,")


def _horizontal(page):
    """Page view without rotated glyphs (the vertical resi text on the label edges)."""
    def keep(o):
        if o.get("object_type") != "char":
            return True
        m = o.get("matrix") or (1, 0, 0, 1, 0, 0)
        return abs(m[1]) < 0.01 and abs(m[2]) < 0.01
    return page.filter(keep)


def _group_lines(words, tol=3.0):
    """words (dicts with top/x0/text/size) -> list of lines (list of words)."""
    lines = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(lines[-1][0]["top"] - w["top"]) <= tol:
            lines[-1].append(w)
        else:
            lines.append([w])
    for ln in lines:
        ln.sort(key=lambda w: w["x0"])
    return lines


def _line_text(ln):
    return " ".join(w["text"] for w in ln)


def _recipient_block(hp, words):
    """Lines of the 'Penerima' column (left column when 'Pengirim' sits beside it)."""
    pen = next((w for w in words if re.match(r"^(penerima|kepada)\b", w["text"], re.I)), None)
    if pen is None:
        return None
    sender = next((w for w in words if re.match(r"^(pengirim|dari)\b", w["text"], re.I)
                   and w["x0"] > pen["x1"] and abs(w["top"] - pen["top"]) < 6), None)
    x1 = (sender["x0"] - 3) if sender else hp.width
    # bottom limit: first "CASHLESS / COD / Berat / Batas Kirim" row under the address
    stops = [w["top"] for w in words
             if w["top"] > pen["top"] + 5 and re.match(r"^(cashless|cod|berat|batas|pengirim|dari)\b", w["text"].rstrip(":"), re.I)]
    bottom = min(stops) - 1 if stops else pen["top"] + 90
    col = [w for w in words if w["x0"] >= pen["x0"] - 3 and w["x0"] < x1
           and pen["top"] - 2 <= w["top"] < bottom]
    lines = _group_lines(col)
    if not lines:
        return None
    base = pen["size"]
    out = []
    for ln in lines:
        # kotak kecamatan/kota (font lebih besar) bukan bagian alamat
        if out and min(w["size"] for w in ln) > base * 1.06:
            break
        out.append(_line_text(ln))
    return out


def _find_recipient(hp, words, lines):
    """(name, phone, address)"""
    block = _recipient_block(hp, words)
    if block is None:
        # fallback: teks baris penuh
        block = []
        for i, l in enumerate(lines):
            if _RECIPIENT_RE.match(l):
                block = [l]
                for nxt in lines[i + 1:]:
                    if _STOP_RE.match(nxt) or len(block) > 8:
                        break
                    block.append(nxt)
                break
    if not block:
        return "", "", ""
    m = _RECIPIENT_RE.match(block[0])
    name_line = (m.group(1) if m else block[0]).strip()
    rest = block[1:]
    if not name_line and rest:
        name_line, rest = rest[0], rest[1:]

    phone = ""
    pm = _PHONE_TAIL_RE.search(name_line)
    if pm:
        phone = re.sub(r"[()\s-]+", "", pm.group(0))
        name_line = name_line[:pm.start()]
    name = _clean(re.sub(r"[()]+", " ", name_line))

    addr = []
    for l in rest:
        if _PHONE_LINE_RE.match(l):
            if not phone:
                phone = re.sub(r"[()\s-]+", "", l)
            continue
        addr.append(l)
    if "*" in phone:       # nomor disamarkan -> tidak berguna
        phone = ""
    return name, phone, _clean(" ".join(addr))


def _find_order_no(text):
    m = re.search(r"(?:no\.?\s*pesanan|nomor\s*pesanan|order\s*(?:id|no\.?|number)|id\s*pesanan)"
                  r"\s*[:#]?\s*([A-Z0-9]{8,20})\b", text, re.I)
    if m:
        return m.group(1).upper()
    m = re.search(r"\bPesan\s*:\s*\(([A-Z0-9]{8,20})\)", text)       # "Pesan: (261106JHYUSCWJ)"
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{6}[A-Z0-9]{6,10})\b", text)
    if m and not m.group(1).isdigit():
        return m.group(1)
    return ""


def _find_courier(text):
    up = text.upper()
    if "GRAB" in up:
        return "GRAB"
    if re.search(r"\bJNE\b", up) or re.search(r"\bJTR\b", up):   # JTR = JNE Trucking
        return "JNE"
    return ""


def _parse_date(text):
    m = re.search(r"(?:batas\s*kirim|kirim\s*sebelum|ship\s*by|tanggal\s*kirim)\s*[:\-]?\s*([^\n]{6,30})", text, re.I)
    chunk = m.group(1) if m else ""
    if not chunk:
        return None
    try:
        d = re.search(r"(\d{1,2})\s*[-/ ]\s*(\d{1,2})\s*[-/ ]\s*(\d{4})", chunk)
        if d:
            return datetime(int(d.group(3)), int(d.group(2)), int(d.group(1)))
        d = re.search(r"(\d{4})-(\d{2})-(\d{2})", chunk)
        if d:
            return datetime(int(d.group(1)), int(d.group(2)), int(d.group(3)))
        d = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})", chunk)
        if d and d.group(2).lower() in _BULAN_MAP:
            return datetime(int(d.group(3)), _BULAN_MAP[d.group(2).lower()], int(d.group(1)))
    except ValueError:
        pass
    return None


def _fmt_date(dt):
    return "%02d %s %d" % (dt.day, BULAN[dt.month - 1], dt.year)


def _find_items(lines):
    """Rows of the product table -> [(nama_produk, qty)]"""
    hdr = next((i for i, l in enumerate(lines)
                if re.search(r"nama\s*produk|product\s*name", l, re.I)), None)
    if hdr is None:
        return []
    items = []
    for l in lines[hdr + 1:]:
        if re.match(r"^\s*(?:pesan|catatan|note|total)\b", l, re.I):
            break
        m = re.match(r"^\s*\d{1,2}[.)]?\s+(.+?)\s+(\d{1,4})\s*$", l)   # "1 GREE ACOOL4 Tower ... 1"
        if m:
            items.append((m.group(1).strip(), int(m.group(2))))
    return items


def simple_name(product, suffix=DEFAULT_SUFFIX):
    """'GREE ACOOL4 Tower Kipas Angin Fan ...' -> 'GREE ACOOL4 (Bubble Wrap)'
    (merek + model saja, tidak usah lengkap)."""
    p = re.sub(r"[\[\(][^\]\)]*[\]\)]", " ", product)           # buang [..] / (..)
    toks = [t for t in re.split(r"\s+", p) if re.search(r"[A-Za-z0-9]", t)]
    short = " ".join(toks[:2]) or product.strip()
    return "%s (%s)" % (short, suffix) if suffix else short


def parse_label_page(page):
    hp = _horizontal(page)
    words = hp.extract_words(x_tolerance=1.5, y_tolerance=3, extra_attrs=["size", "fontname"])
    lines = [_line_text(l) for l in _group_lines(words)]
    text = "\n".join(lines)
    name, phone, address = _find_recipient(hp, words, lines)
    dt = _parse_date(text)
    return {
        "order_no": _find_order_no(text),
        "pic": name, "phone": phone, "alamat": address,
        "via": _find_courier(text),
        "tgl": _fmt_date(dt) if dt else "",
        "items": _find_items(lines),
        "has_recipient": bool(name or address),
    }


def extract_orders(input_bytes, suffix=DEFAULT_SUFFIX, via_default="GRAB / JNE"):
    """Read every page of a label PDF -> list of orders. Pages of the same order
    number are merged."""
    now = datetime.now(WIB)
    today = _fmt_date(now)
    # GRAB: harus siap dipickup 30 menit setelah file diupload
    pickup = "Harus sudah bisa dipickup Jam %s - Hari Ini" % (now + timedelta(minutes=30)).strftime("%H:%M")
    pages = []
    with pdfplumber.open(BytesIO(input_bytes)) as pdf:
        for i, pg in enumerate(pdf.pages):
            try:
                d = parse_label_page(pg)
            except Exception:
                traceback.print_exc()
                continue
            d["page"] = i + 1
            pages.append(d)

    merged, index = [], {}
    for d in pages:
        key = d["order_no"] or ("_p%d" % d["page"])
        if d["order_no"] and key in index:
            cur = merged[index[key]]
            for f in ("pic", "phone", "alamat", "via", "tgl"):
                if not cur[f] and d[f]:
                    cur[f] = d[f]
            if d["items"] and not cur["items"]:
                cur["items"] = d["items"]
            continue
        index[key] = len(merged)
        merged.append(d)

    out = []
    for d in merged:
        if not (d["order_no"] or d["has_recipient"]):
            continue
        items = [{"unit": simple_name(n, suffix), "qty": q} for n, q in d["items"]] \
            or [{"unit": "", "qty": 1}]
        via = d["via"] or via_default
        tgl = d["tgl"] or today
        out.append({
            "pembeli": "SHOPEE - " + d["order_no"],
            "alamat": d["alamat"],
            "pic": d["pic"] + ((" (%s)" % d["phone"]) if d["phone"] else ""),
            "via": via,
            "tgl": tgl,
            "pickup": pickup,
            "kirim_tgl": pickup if via == "GRAB" else tgl,
            "items": items,
            "page": d["page"],
            "check": [k for k, v in (("order_no", d["order_no"]), ("alamat", d["alamat"]),
                                     ("pic", d["pic"]), ("tgl", d["tgl"]),
                                     ("items", d["items"])) if not v],
        })
    return out


# ---------------------------------------------------------------------------
# PDF surat jalan sederhana, 100 x 100 mm (1:1), 1 halaman per pesanan
# ---------------------------------------------------------------------------

def _wrap(text, font, size, width):
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if stringWidth(t, font, size) <= width:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


def build_suratjalan_pdf(orders):
    buf = BytesIO()
    S = mm_to_pt(100)
    c = rl_canvas.Canvas(buf, pagesize=(S, S))
    c.setTitle("Surat Jalan")
    lm = mm_to_pt(6)
    val_x = lm + 84          # kolom nilai (label terpanjang "NAMA PEMBELI" muat)
    val_w = S - val_x - lm

    for o in orders:
        rows = [("NAMA PEMBELI", o.get("pembeli", "")),
                ("ALAMAT KIRIM", o.get("alamat", "")),
                ("PIC PENERIMA", o.get("pic", "")),
                ("KIRIM VIA", o.get("via", "")),
                ("KIRIM TGL", o.get("kirim_tgl") or o.get("tgl", ""))]
        items = o.get("items") or []
        for it in items:
            rows.append(("UNIT", it.get("unit", "")))
            rows.append(("QTY", str(it.get("qty", ""))))

        # pilih ukuran font terbesar yang masih muat di 100 mm
        size = 11
        while size > 6:
            n = sum(len(_wrap(v, "Helvetica", size, val_w)) for _, v in rows)
            if n * (size + 2.5) + len(rows) * 5 + 30 <= S - 2 * lm:
                break
            size -= 0.5
        lead = size + 2.5

        y = S - lm - 8
        c.setFont("Helvetica-Bold", size + 3)
        c.drawString(lm, y, "SURAT JALAN")
        y -= 6
        c.setLineWidth(1)
        c.line(lm, y, S - lm, y)
        y -= lead + 6
        for label, value in rows:
            lsz = min(size, 8.5)
            c.setFont("Helvetica-Bold", lsz)
            c.drawString(lm, y, label)
            c.drawString(val_x - 8, y, ":")
            c.setFont("Helvetica", size)
            wl = _wrap(value, "Helvetica", size, val_w)
            for k, ln in enumerate(wl):
                c.drawString(val_x, y - k * lead, ln)
            y -= lead * len(wl) + 5
        c.showPage()
    c.save()
    return buf.getvalue()


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

  .tpl-single {
    margin-top: 16px;
    border: 1px solid var(--accent);
    background: rgba(255,106,61,0.08);
    border-radius: 10px;
    padding: 12px 14px;
  }
  .tpl-single label { margin: 0; cursor: pointer; color: var(--text); font-size: 0.9rem; display: flex; align-items: center; gap: 10px; }
  .tpl-single input { width: 17px; height: 17px; accent-color: var(--accent); flex: none; }
  .tpl-single small { display: block; color: var(--muted); font-size: 0.76rem; margin-top: 2px; }


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


  .sj-order { border:1px solid var(--border); border-radius:12px; padding:14px; margin-top:14px; background:#12162a; }
  .sj-order h3 { margin:0 0 10px; font-size:.9rem; color:var(--muted); font-weight:600; }
  .sj-order .row { display:grid; grid-template-columns:110px 1fr; gap:8px; align-items:center; margin-bottom:8px; }
  .sj-order .row span { font-size:.78rem; color:var(--muted); font-weight:600; }
  .sj-order input, .sj-order textarea, .sj-order select { width:100%; padding:8px 10px; border-radius:8px; border:1px solid var(--border); background:#0f1329; color:var(--text); font:inherit; font-size:.9rem; }
  .sj-order .warn { border-color:var(--err); }
  .sj-order textarea { resize:vertical; min-height:56px; }
  .sj-text { width:100%; font-family:ui-monospace,Menlo,Consolas,monospace; font-size:.85rem; line-height:1.5; }
  .sj-copy { margin:8px 0 4px; padding:8px 14px; border-radius:8px; border:1px solid var(--accent); background:transparent; color:var(--text); cursor:pointer; font-size:.85rem; }
  .sj-copy:hover { background:rgba(255,106,61,0.12); }
  .sj-sub { margin:14px 0 8px; font-size:.78rem; color:var(--muted); font-weight:600; text-transform:uppercase; letter-spacing:.04em; }
  .sj-item { display:grid; grid-template-columns:1fr 70px; gap:8px; margin-bottom:6px; }
  @media (max-width: 560px) { .sj-order .row { grid-template-columns:1fr; } }
  footer { text-align: center; color: var(--muted); font-size: 0.78rem; margin-top: 28px; }
  @media (max-width: 560px) { .grid { grid-template-columns: 1fr; } .mode-toggle { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>&#128208; PDF Resizer &amp; Label Fitter</h1>
    </header>

  <form id="form" class="card">
    <div class="dropzone" id="dropzone">
      <div>&#128196; Klik atau seret file <strong>PDF</strong> ke sini</div>
      <div id="fileInfo"></div>
      <input type="file" id="fileInput" name="file" accept="application/pdf" />
    </div>

    <div class="tpl-single">
      <label>
        <input type="checkbox" id="tplLabel" />
        <span>Pakai template label 100&times;100 mm
          <small>Set otomatis: 100&times;100 mm &middot; mode Fit (jaga rasio) &middot; margin 2 mm &middot; auto-crop aktif</small>
        </span>
      </label>
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

  <div class="card" id="sjCard">
    <h2 style="margin:0 0 4px;font-size:1.15rem;">&#129534; Teks &amp; PDF 100&times;100 dari label Shopee</h2>
    <p style="margin:0 0 16px;color:var(--muted);font-size:0.85rem;">Unggah PDF label (asli atau hasil resize), hasilnya berupa teks siap salin dan PDF 1:1.</p>
    <div class="dropzone" id="sjDrop"><div>&#128196; Klik atau seret <strong>PDF label</strong> ke sini</div><div id="sjFileInfo" style="margin-top:10px;font-size:.9rem;color:var(--ok);display:none"></div>
      <input type="file" id="sjFile" accept="application/pdf" /></div>
    <div class="grid">
      <div><label for="sjVia">Kirim via (default)</label>
        <select id="sjVia"><option>GRAB</option><option>JNE</option><option>GRAB / JNE</option></select></div>
      <div><label for="sjSuffix">Keterangan kemasan</label>
        <input type="text" id="sjSuffix" value="Bubble Wrap" style="width:100%;padding:10px 12px;border-radius:8px;border:1px solid var(--border);background:#0f1329;color:var(--text);" /></div>
    </div>
    <button type="button" class="submit-btn" id="sjRead">Baca Label</button>
    <div id="sjStatus" style="margin-top:14px;font-size:.88rem;text-align:center;color:var(--muted);min-height:20px"></div>
    <div id="sjList"></div>
    <button type="button" class="submit-btn" id="sjDownload" style="display:none">Download PDF 100&times;100 mm</button>
  </div>

  <footer>
    @ Yudha Pradipta
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
    setMode(btn.dataset.mode);
    untickTemplate();
  });
});

/* ------- Satu checkbox template: isi semua setting sekaligus ------------- */

const tplLabel = document.getElementById('tplLabel');
const TEMPLATE_LABEL = { w: 100, h: 100, margin: 2, mode: 'fit', autocrop: true, nooverflow: true };

function setMode(m) {
  mode = m;
  document.querySelectorAll('.mode-toggle button').forEach(b => {
    b.classList.toggle('active', b.dataset.mode === m);
  });
}

function applySettings(s) {
  document.getElementById('width').value = s.w;
  document.getElementById('height').value = s.h;
  document.getElementById('margin').value = s.margin;
  document.getElementById('autocrop').checked = !!s.autocrop;
  document.getElementById('nooverflow').checked = s.nooverflow !== false;
  setMode(s.mode);
}

tplLabel.addEventListener('change', () => {
  if (tplLabel.checked) applySettings(TEMPLATE_LABEL);
});

// Kalau user mengubah setting manual, centang template dilepas otomatis.
function untickTemplate() { tplLabel.checked = false; }
['width', 'height', 'margin', 'autocrop', 'nooverflow'].forEach(id => {
  document.getElementById(id).addEventListener('input', untickTemplate);
  document.getElementById(id).addEventListener('change', untickTemplate);
});
document.querySelectorAll('.presets button').forEach(b => b.addEventListener('click', untickTemplate));

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
    statusEl.textContent = '\\u2713 Selesai! PDF hasil sudah terconvert.';
  } catch (err) {
    statusEl.className = 'err';
    statusEl.textContent = err.message;
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Convert & Download PDF';
  }
});

/* ------------------ Surat Jalan ------------------ */
const sjFile = document.getElementById('sjFile');
const sjDrop = document.getElementById('sjDrop');
const sjStatus = document.getElementById('sjStatus');
const sjList = document.getElementById('sjList');
const sjDownload = document.getElementById('sjDownload');
let sjOrders = [];

sjDrop.addEventListener('click', () => sjFile.click());
sjDrop.addEventListener('dragover', (e) => { e.preventDefault(); sjDrop.classList.add('dragover'); });
sjDrop.addEventListener('dragleave', () => sjDrop.classList.remove('dragover'));
sjDrop.addEventListener('drop', (e) => {
  e.preventDefault(); sjDrop.classList.remove('dragover');
  if (e.dataTransfer.files.length) { sjFile.files = e.dataTransfer.files; sjShowName(); }
});
sjFile.addEventListener('change', sjShowName);
function sjShowName() {
  const el = document.getElementById('sjFileInfo');
  if (sjFile.files.length) { el.style.display = 'block'; el.textContent = '\u2713 ' + sjFile.files[0].name; }
}
function sjMsg(t, cls) { sjStatus.className = cls || ''; sjStatus.style.color = cls === 'err' ? 'var(--err)' : cls === 'ok' ? 'var(--ok)' : 'var(--muted)'; sjStatus.textContent = t; }

function mk(tag, attrs, parent) {
  const el = document.createElement(tag);
  Object.assign(el, attrs || {});
  if (parent) parent.appendChild(el);
  return el;
}

function orderText(o) {
  const L = [
    'NAMA PEMBELI: ' + (o.pembeli || ''),
    'ALAMAT KIRIM: ' + (o.alamat || ''),
    'PIC PENERIMA: ' + (o.pic || ''),
    'KIRIM VIA: ' + (o.via || ''),
    'KIRIM TGL: ' + (o.kirim_tgl || ''),
    ''
  ];
  (o.items || []).forEach(it => L.push('UNIT: ' + (it.unit || '') + ' - ' + (it.qty || '')));
  return L.join('\\n');
}

function renderOrders() {
  sjList.textContent = '';
  sjOrders.forEach((o, i) => {
    const box = mk('div', {className: 'sj-order'}, sjList);
    mk('h3', {textContent: 'Pesanan ' + (i + 1) + ' · halaman ' + o.page}, box);

    // --- output teks (bisa dicopy) ---
    const ta = mk('textarea', {className: 'sj-text', readOnly: false, rows: 9}, box);
    const copyBtn = mk('button', {type: 'button', className: 'sj-copy', textContent: 'Salin teks'}, box);
    const refresh = () => { ta.value = orderText(o); };
    copyBtn.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(ta.value); }
      catch (e) { ta.select(); document.execCommand('copy'); }
      copyBtn.textContent = '✓ Tersalin';
      setTimeout(() => { copyBtn.textContent = 'Salin teks'; }, 1500);
    });

    // --- field edit ---
    mk('div', {className: 'sj-sub', textContent: 'Edit hasil bacaan'}, box);
    const inputs = {};
    const field = (label, key, kind) => {
      const row = mk('div', {className: 'row'}, box);
      mk('span', {textContent: label}, row);
      let inp;
      if (kind === 'area') inp = mk('textarea', {value: o[key] || ''}, row);
      else if (kind === 'via') {
        inp = mk('select', {}, row);
        ['GRAB', 'JNE', 'GRAB / JNE'].forEach(v => inp.appendChild(new Option(v, v)));
        inp.value = o[key];
      } else inp = mk('input', {type: 'text', value: o[key] || ''}, row);
      if (!o[key]) inp.classList.add('warn');
      inp.addEventListener('input', () => { o[key] = inp.value; inp.classList.toggle('warn', !inp.value); refresh(); });
      inputs[key] = inp;
    };
    field('NAMA PEMBELI', 'pembeli');
    field('ALAMAT KIRIM', 'alamat', 'area');
    field('PIC PENERIMA', 'pic');
    field('KIRIM VIA', 'via', 'via');
    field('KIRIM TGL', 'kirim_tgl');
    // GRAB -> "Harus sudah bisa dipickup Jam ..."; selain itu -> Batas Kirim di label
    inputs.via.addEventListener('change', () => {
      o.kirim_tgl = (o.via === 'GRAB') ? o.pickup : o.tgl;
      inputs.kirim_tgl.value = o.kirim_tgl;
      refresh();
    });
    const row = mk('div', {className: 'row'}, box);
    mk('span', {textContent: 'UNIT / QTY'}, row);
    const wrap = mk('div', {}, row);
    o.items.forEach(it => {
      const line = mk('div', {className: 'sj-item'}, wrap);
      const u = mk('input', {type: 'text', value: it.unit}, line);
      const q = mk('input', {type: 'number', min: 1, value: it.qty}, line);
      u.addEventListener('input', () => { it.unit = u.value; refresh(); });
      q.addEventListener('input', () => { it.qty = parseInt(q.value || '1', 10); refresh(); });
    });
    refresh();
  });
  sjDownload.style.display = sjOrders.length ? 'block' : 'none';
}

document.getElementById('sjRead').addEventListener('click', async () => {
  if (!sjFile.files.length) { sjMsg('Pilih PDF label terlebih dahulu.', 'err'); return; }
  const fd = new FormData();
  fd.append('file', sjFile.files[0]);
  fd.append('via', document.getElementById('sjVia').value);
  fd.append('suffix', document.getElementById('sjSuffix').value);
  sjMsg('Membaca label...');
  try {
    const res = await fetch('/api/extract', { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Gagal membaca PDF.');
    sjOrders = data.orders;
    renderOrders();
    const bad = sjOrders.filter(o => o.check.length).length;
    sjMsg('\u2713 ' + sjOrders.length + ' pesanan terbaca.' + (bad ? ' ' + bad + ' perlu dicek (kolom merah).' : ''), bad ? '' : 'ok');
  } catch (err) { sjMsg(err.message, 'err'); }
});

sjDownload.addEventListener('click', async () => {
  sjDownload.disabled = true;
  try {
    const res = await fetch('/api/suratjalan', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({orders: sjOrders}) });
    if (!res.ok) throw new Error('Gagal membuat PDF.');
    const url = URL.createObjectURL(await res.blob());
    const a = mk('a', {href: url, download: 'surat_jalan.pdf'}, document.body);
    a.click(); a.remove(); URL.revokeObjectURL(url);
    sjMsg('\u2713 PDF terunduh.', 'ok');
  } catch (err) { sjMsg(err.message, 'err'); }
  finally { sjDownload.disabled = false; }
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


@app.route("/api/extract", methods=["POST"])
def extract():
    try:
        f = request.files.get("file")
        if not f or not f.filename.lower().endswith(".pdf"):
            return jsonify({"error": "Unggah file .pdf label Shopee."}), 400
        suffix = (request.form.get("suffix") if "suffix" in request.form else DEFAULT_SUFFIX).strip()
        via = (request.form.get("via") or "GRAB / JNE").strip()
        orders = extract_orders(f.read(), suffix, via)
        if not orders:
            return jsonify({"error": "Tidak ada data pesanan yang terbaca. Pastikan PDF berisi teks (bukan hasil scan/gambar)."}), 422
        return jsonify({"orders": orders})
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Gagal membaca PDF."}), 500


@app.route("/api/suratjalan", methods=["POST"])
def suratjalan():
    try:
        payload = request.get_json(silent=True) or {}
        orders = payload.get("orders") or []
        if not orders:
            return jsonify({"error": "Tidak ada data untuk dicetak."}), 400
        return send_file(BytesIO(build_suratjalan_pdf(orders)), mimetype="application/pdf",
                         as_attachment=True, download_name="surat_jalan.pdf")
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Gagal membuat PDF."}), 500


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# Local dev entrypoint: `python3 api/index.py`
if __name__ == "__main__":
    app.run(debug=True, port=5000)
