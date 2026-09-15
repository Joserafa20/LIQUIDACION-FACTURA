import io
import json
import re
import threading
import time
from pathlib import Path
from werkzeug.utils import secure_filename

import fitz
import pdfplumber
from flask import (
    Flask, jsonify, redirect, render_template, request,
    send_file, url_for, flash
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR   = Path(__file__).parent
PDF_DIR    = BASE_DIR / "pdfs"
INDEX_FILE = BASE_DIR / "index.json"
PDF_DIR.mkdir(exist_ok=True)

CATASTRAL_RE   = re.compile(r"\b(\d{15})\b")
LONG_DIGIT_RE  = re.compile(r"^\d{9,}$")
ALLOWED_EXT    = {".pdf"}

app = Flask(__name__)
app.secret_key = "predial-sabanalarga-2026"

# ---------------------------------------------------------------------------
# Index — persisted to index.json
# { catastral_ref: { "file": "filename.pdf", "page": 0 } }
# ---------------------------------------------------------------------------
_index: dict = {}
_index_lock = threading.Lock()
_processing: dict = {}   # filename -> { status, pages_done, total, error }


def _load_index():
    global _index
    if INDEX_FILE.exists():
        with open(INDEX_FILE, encoding="utf-8") as f:
            _index = json.load(f)
    else:
        _index = {}


def _save_index():
    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(_index, f, ensure_ascii=False)


def _catastral_from_words(page) -> str | None:
    threshold = page.height * 0.40
    candidates = []
    for w in page.extract_words():
        txt = w["text"]
        if LONG_DIGIT_RE.match(txt) and w["top"] < threshold and len(txt) >= 12:
            candidates.append((len(txt), txt))
    if not candidates:
        return None
    candidates.sort(key=lambda x: -x[0])
    return candidates[0][1]


def _process_pdf(pdf_path: Path):
    """Index one PDF in a background thread. Updates _processing status."""
    fname = pdf_path.name
    _processing[fname] = {"status": "processing", "pages_done": 0, "total": 0, "error": None}
    try:
        with pdfplumber.open(pdf_path) as pdf:
            total = len(pdf.pages)
            _processing[fname]["total"] = total
            local = {}
            for i, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                matches = CATASTRAL_RE.findall(text)
                ref = matches[0] if matches else _catastral_from_words(page)
                if ref:
                    local[ref] = {"file": fname, "page": i}
                _processing[fname]["pages_done"] = i + 1

        with _index_lock:
            _index.update(local)
            _save_index()

        _processing[fname]["status"] = "done"
        _processing[fname]["indexed"] = len(local)
    except Exception as exc:
        _processing[fname]["status"] = "error"
        _processing[fname]["error"] = str(exc)


def _lookup(ref: str):
    """Return index entry or None. Tries exact match then leading-zero tolerance."""
    ref = ref.strip().replace(" ", "").replace("-", "")
    with _index_lock:
        if ref in _index:
            return ref, _index[ref]
        stripped = ref.lstrip("0")
        if stripped:
            for stored, entry in _index.items():
                if stored.lstrip("0") == stripped:
                    return stored, entry
    return None, None


# ---------------------------------------------------------------------------
# Boot: load index and auto-index the bundled PDF if present
# ---------------------------------------------------------------------------
BUNDLED = Path(r"C:\Users\LENOVO\Downloads\impuestos.facturacion.adup_facturarango.pdf")

_load_index()

if not _index and BUNDLED.exists():
    import shutil
    dest = PDF_DIR / BUNDLED.name
    if not dest.exists():
        shutil.copy2(BUNDLED, dest)
    print("Indexando PDF inicial…")
    _process_pdf(dest)
    print(f"Listo: {len(_index)} facturas indexadas.")
elif _index:
    print(f"Índice cargado: {len(_index)} facturas.")


# ---------------------------------------------------------------------------
# Routes — Contribuyente
# ---------------------------------------------------------------------------
@app.route("/")
def contribuyente():
    return render_template("contribuyente.html")


@app.route("/buscar")
def buscar():
    ref = request.args.get("ref", "")
    _, entry = _lookup(ref)
    if entry:
        return jsonify({"found": True, "page": entry["page"] + 1})
    return jsonify({"found": False})


@app.route("/imagen")
def imagen():
    ref = request.args.get("ref", "")
    _, entry = _lookup(ref)
    if not entry:
        return "No encontrado", 404
    pdf_path = PDF_DIR / entry["file"]
    if not pdf_path.exists():
        return "Archivo no disponible", 404
    doc = fitz.open(pdf_path)
    page = doc[entry["page"]]
    mat = fitz.Matrix(180 / 72, 180 / 72)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    doc.close()
    return send_file(io.BytesIO(pix.tobytes("png")), mimetype="image/png")


# ---------------------------------------------------------------------------
# Routes — Admin
# ---------------------------------------------------------------------------
@app.route("/admin")
def admin():
    with _index_lock:
        total = len(_index)
        files = {}
        for entry in _index.values():
            files[entry["file"]] = files.get(entry["file"], 0) + 1
    return render_template("admin.html",
                           total=total,
                           files=files,
                           processing=dict(_processing))


@app.route("/admin/upload", methods=["POST"])
def admin_upload():
    uploaded = request.files.getlist("pdfs")
    if not uploaded:
        flash("No seleccionaste ningún archivo.", "error")
        return redirect(url_for("admin"))

    launched = []
    for f in uploaded:
        if not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            flash(f"'{f.filename}' no es un PDF válido, se omitió.", "warning")
            continue
        fname = secure_filename(f.filename)
        dest = PDF_DIR / fname
        f.save(dest)
        t = threading.Thread(target=_process_pdf, args=(dest,), daemon=True)
        t.start()
        launched.append(fname)

    if launched:
        flash(f"Procesando: {', '.join(launched)}", "ok")
    return redirect(url_for("admin"))


@app.route("/admin/status")
def admin_status():
    with _index_lock:
        total = len(_index)
        files = {}
        for entry in _index.values():
            files[entry["file"]] = files.get(entry["file"], 0) + 1
    return jsonify({
        "total": total,
        "files": files,
        "processing": _processing,
    })


@app.route("/admin/delete", methods=["POST"])
def admin_delete():
    fname = request.form.get("file", "")
    if not fname:
        flash("Archivo no especificado.", "error")
        return redirect(url_for("admin"))
    removed = 0
    with _index_lock:
        keys_to_del = [k for k, v in _index.items() if v["file"] == fname]
        for k in keys_to_del:
            del _index[k]
            removed += 1
        _save_index()
    pdf_path = PDF_DIR / secure_filename(fname)
    if pdf_path.exists():
        pdf_path.unlink()
    if fname in _processing:
        del _processing[fname]
    flash(f"'{fname}' eliminado — {removed} facturas removidas del índice.", "ok")
    return redirect(url_for("admin"))


if __name__ == "__main__":
    app.run(debug=False, port=8080, host="0.0.0.0")
