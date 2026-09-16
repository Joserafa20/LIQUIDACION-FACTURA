import io
import json
import os
import re
import threading
from pathlib import Path
from werkzeug.utils import secure_filename

import boto3
from botocore.config import Config
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

# R2 config (set these as env vars on Render)
R2_ACCOUNT_ID      = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID   = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME     = os.environ.get("R2_BUCKET_NAME", "facturas-prediales")
R2_PUBLIC_URL      = os.environ.get("R2_PUBLIC_URL", "").rstrip("/")
R2_ENDPOINT_URL    = os.environ.get(
    "R2_ENDPOINT_URL",
    f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com" if R2_ACCOUNT_ID else ""
)

app = Flask(__name__)
app.secret_key = "predial-sabanalarga-2026"

# ---------------------------------------------------------------------------
# R2 client
# ---------------------------------------------------------------------------
def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def _upload_to_r2(key: str, data: bytes, content_type: str = "image/jpeg"):
    s3 = _r2_client()
    s3.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=data,
        ContentType=content_type,
    )


def _public_url(key: str) -> str:
    return f"{R2_PUBLIC_URL}/{key}"


# ---------------------------------------------------------------------------
# Index — persisted to R2 as index.json
# { catastral_ref: "images/pXXXXX.jpg" }   (public URL path)
# ---------------------------------------------------------------------------
_index: dict = {}
_index_lock = threading.Lock()
_processing: dict = {}


def _load_index():
    global _index
    if not R2_ENDPOINT_URL or not R2_ACCESS_KEY_ID:
        print("R2 not configured — index empty.")
        _index = {}
        return
    try:
        s3 = _r2_client()
        obj = s3.get_object(Bucket=R2_BUCKET_NAME, Key="index.json")
        _index = json.loads(obj["Body"].read().decode("utf-8"))
        print(f"Index loaded from R2: {len(_index)} facturas.")
    except s3.exceptions.NoSuchKey:
        _index = {}
    except Exception as exc:
        print(f"Could not load index from R2: {exc}")
        _index = {}


def _save_index():
    if not R2_ENDPOINT_URL or not R2_ACCESS_KEY_ID:
        return
    try:
        data = json.dumps(_index, ensure_ascii=False).encode("utf-8")
        _upload_to_r2("index.json", data, content_type="application/json")
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


def _process_pdf(pdf_path: Path):
    """Index one PDF: render pages as JPEG, upload to R2, update index."""
    fname = pdf_path.name
    _processing[fname] = {"status": "processing", "pages_done": 0, "total": 0, "error": None}
    try:
        doc_fitz = fitz.open(pdf_path)
        with pdfplumber.open(pdf_path) as pdf:
            total = len(pdf.pages)
            _processing[fname]["total"] = total
            local = {}
            for i, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                matches = CATASTRAL_RE.findall(text)
                ref = matches[0] if matches else _catastral_from_words(page)
                if ref:
                    # Render page as JPEG
                    fitz_page = doc_fitz[i]
                    mat = fitz.Matrix(120 / 72, 120 / 72)
                    pix = fitz_page.get_pixmap(matrix=mat, alpha=False)
                    jpeg_bytes = pix.tobytes("jpeg", jpg_quality=82)

                    key = f"images/{ref}.jpg"
                    _upload_to_r2(key, jpeg_bytes)
                    local[ref] = key
                _processing[fname]["pages_done"] = i + 1

        doc_fitz.close()

        with _index_lock:
            _index.update(local)
            _save_index()

        _processing[fname]["status"] = "done"
        _processing[fname]["indexed"] = len(local)
    except Exception as exc:
        _processing[fname]["status"] = "error"
        _processing[fname]["error"] = str(exc)
    finally:
        # Remove uploaded PDF from ephemeral disk
        try:
            pdf_path.unlink(missing_ok=True)
        except Exception:
            pass


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
    stored_ref, key = _lookup(ref)
    if key:
        return jsonify({"found": True, "url": _public_url(key)})
    return jsonify({"found": False})


@app.route("/imagen")
def imagen():
    """Kept for backwards compatibility — redirects to R2 public URL."""
    ref = request.args.get("ref", "")
    _, key = _lookup(ref)
    if not key:
        return "No encontrado", 404
    return redirect(_public_url(key))


# ---------------------------------------------------------------------------
# Routes — Admin
# ---------------------------------------------------------------------------
@app.route("/admin")
def admin():
    with _index_lock:
        total = len(_index)
        files: dict[str, int] = {}
        for entry_key in _index.values():
            # entry_key is like "images/ref.jpg" — group by "uploaded PDF"
            # Since we now store by ref, count all as one logical group
            files["R2 Storage"] = files.get("R2 Storage", 0) + 1
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
        files: dict[str, int] = {}
        for entry_key in _index.values():
            files["R2 Storage"] = files.get("R2 Storage", 0) + 1
    return jsonify({
        "total": total,
        "files": files,
        "processing": _processing,
    })


@app.route("/admin/delete", methods=["POST"])
def admin_delete():
    fname = request.form.get("file", "")
    flash("Para eliminar facturas individualmente contactá al administrador.", "warning")
    return redirect(url_for("admin"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(debug=False, port=port, host="0.0.0.0")
