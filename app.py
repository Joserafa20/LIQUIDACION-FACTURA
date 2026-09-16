import io
import json
import os
import re
import threading
from pathlib import Path
from werkzeug.utils import secure_filename

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import fitz
import pdfplumber
from flask import (
    Flask, jsonify, redirect, render_template, request,
    send_file, url_for, flash
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR  = Path(__file__).parent
PDF_DIR   = BASE_DIR / "pdfs"
PDF_DIR.mkdir(exist_ok=True)

CATASTRAL_RE  = re.compile(r"\b(\d{15})\b")
LONG_DIGIT_RE = re.compile(r"^\d{9,}$")
ALLOWED_EXT   = {".pdf"}

R2_ACCOUNT_ID        = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID     = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME       = os.environ.get("R2_BUCKET_NAME", "facturas-prediales")
R2_PUBLIC_URL        = os.environ.get("R2_PUBLIC_URL", "").rstrip("/")
R2_ENDPOINT_URL      = os.environ.get(
    "R2_ENDPOINT_URL",
    f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else ""
)

app = Flask(__name__)
app.secret_key = "predial-sabanalarga-2026"

# ---------------------------------------------------------------------------
# R2 helpers
# ---------------------------------------------------------------------------
def _r2():
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def _r2_put(key: str, data: bytes, content_type: str = "application/octet-stream"):
    _r2().put_object(Bucket=R2_BUCKET_NAME, Key=key, Body=data, ContentType=content_type)


def _r2_get_bytes(key: str) -> bytes:
    obj = _r2().get_object(Bucket=R2_BUCKET_NAME, Key=key)
    return obj["Body"].read()


def _r2_exists(key: str) -> bool:
    try:
        _r2().head_object(Bucket=R2_BUCKET_NAME, Key=key)
        return True
    except ClientError:
        return False


def _pub(key: str) -> str:
    return f"{R2_PUBLIC_URL}/{key}"


# ---------------------------------------------------------------------------
# Index  { catastral_ref: {"file": "name.pdf", "page": 0} }
# Stored as index.json in R2
# ---------------------------------------------------------------------------
_index: dict = {}
_index_lock = threading.Lock()
_processing: dict = {}


def _load_index():
    global _index
    if not (R2_ENDPOINT_URL and R2_ACCESS_KEY_ID):
        print("R2 not configured — index empty.")
        _index = {}
        return
    try:
        data = _r2_get_bytes("index.json")
        _index = json.loads(data.decode("utf-8"))
        print(f"Index loaded from R2: {len(_index)} facturas.")
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            _index = {}
        else:
            print(f"R2 error loading index: {e}")
            _index = {}
    except Exception as exc:
        print(f"Could not load index: {exc}")
        _index = {}


def _save_index():
    try:
        _r2_put("index.json", json.dumps(_index, ensure_ascii=False).encode(), "application/json")
    except Exception as exc:
        print(f"Could not save index to R2: {exc}")


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


# ---------------------------------------------------------------------------
# Background processing — text-only indexing + save PDF to R2
# Rendering is done lazily on first search request
# ---------------------------------------------------------------------------
def _process_pdf(pdf_path: Path):
    fname = pdf_path.name
    _processing[fname] = {"status": "processing", "pages_done": 0, "total": 0, "error": None}
    try:
        # Upload original PDF to R2 for later on-demand rendering
        pdf_bytes = pdf_path.read_bytes()
        _r2_put(f"pdfs/{fname}", pdf_bytes, "application/pdf")
        print(f"PDF uploaded to R2: pdfs/{fname}")

        # Text extraction only (fast — no rendering)
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
        print(f"Done indexing {fname}: {len(local)} facturas.")
    except Exception as exc:
        _processing[fname]["status"] = "error"
        _processing[fname]["error"] = str(exc)
        print(f"Error processing {fname}: {exc}")
    finally:
        try:
            pdf_path.unlink(missing_ok=True)
        except Exception:
            pass


def _render_and_cache(file: str, page_num: int, ref: str) -> str | None:
    """Render one page from R2-stored PDF, cache as JPEG in R2, return public URL."""
    img_key = f"images/{ref}.jpg"
    if _r2_exists(img_key):
        return _pub(img_key)
    try:
        pdf_bytes = _r2_get_bytes(f"pdfs/{file}")
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        fitz_page = doc[page_num]
        mat = fitz.Matrix(120 / 72, 120 / 72)
        pix = fitz_page.get_pixmap(matrix=mat, alpha=False)
        jpeg_bytes = pix.tobytes("jpeg", jpg_quality=82)
        doc.close()
        _r2_put(img_key, jpeg_bytes, "image/jpeg")
        return _pub(img_key)
    except Exception as exc:
        print(f"Error rendering {file} page {page_num}: {exc}")
        return None


def _lookup(ref: str):
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
# Boot
# ---------------------------------------------------------------------------
_load_index()

# ---------------------------------------------------------------------------
# Routes — Contribuyente
# ---------------------------------------------------------------------------
@app.route("/")
def contribuyente():
    return render_template("contribuyente.html")


@app.route("/buscar")
def buscar():
    ref = request.args.get("ref", "")
    stored_ref, entry = _lookup(ref)
    if not entry:
        return jsonify({"found": False})
    return jsonify({"found": True, "ref": stored_ref})


@app.route("/imagen")
def imagen():
    """Render page on demand (cached in R2 after first request)."""
    ref = request.args.get("ref", "")
    stored_ref, entry = _lookup(ref)
    if not entry:
        return "No encontrado", 404
    url = _render_and_cache(entry["file"], entry["page"], stored_ref)
    if not url:
        return "Error al generar la imagen", 500
    return redirect(url)


# ---------------------------------------------------------------------------
# Routes — Admin
# ---------------------------------------------------------------------------
@app.route("/admin")
def admin():
    with _index_lock:
        total = len(_index)
        files: dict[str, int] = {}
        for entry in _index.values():
            fname = entry["file"]
            files[fname] = files.get(fname, 0) + 1
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
        if Path(f.filename).suffix.lower() not in ALLOWED_EXT:
            flash(f"'{f.filename}' no es un PDF válido.", "warning")
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
        files: dict[str, int] = {}
        for entry in _index.values():
            fname = entry["file"]
            files[fname] = files.get(fname, 0) + 1
    return jsonify({"total": total, "files": files, "processing": _processing})


@app.route("/admin/delete", methods=["POST"])
def admin_delete():
    fname = request.form.get("file", "")
    if not fname:
        flash("Archivo no especificado.", "error")
        return redirect(url_for("admin"))
    removed = 0
    with _index_lock:
        keys_to_del = [k for k, v in _index.items() if v.get("file") == fname]
        for k in keys_to_del:
            del _index[k]
            removed += 1
        _save_index()
    flash(f"'{fname}' eliminado del índice — {removed} facturas removidas.", "ok")
    return redirect(url_for("admin"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(debug=False, port=port, host="0.0.0.0")
