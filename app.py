import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from werkzeug.utils import secure_filename

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import fitz
from flask import (
    Flask, jsonify, redirect, render_template, request,
    url_for, flash
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR  = Path(__file__).parent
PDF_DIR   = BASE_DIR / "pdfs"
PDF_DIR.mkdir(exist_ok=True)

CATASTRAL_RE = re.compile(r"\b(\d{15})\b")
ALLOWED_EXT  = {".pdf"}

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
    return _r2().get_object(Bucket=R2_BUCKET_NAME, Key=key)["Body"].read()


def _r2_exists(key: str) -> bool:
    try:
        _r2().head_object(Bucket=R2_BUCKET_NAME, Key=key)
        return True
    except ClientError:
        return False


def _pub(key: str) -> str:
    return f"{R2_PUBLIC_URL}/{key}"


# ---------------------------------------------------------------------------
# Index — loaded from R2 on startup
# ---------------------------------------------------------------------------
_index: dict = {}
_index_lock  = threading.Lock()

# Processing status: fname -> {"status", "pages_done", "total", "error", "status_file"}
_processing: dict = {}


def _load_index():
    global _index
    if not (R2_ENDPOINT_URL and R2_ACCESS_KEY_ID):
        print("R2 not configured — index empty.")
        _index = {}
        return
    try:
        data   = _r2_get_bytes("index.json")
        _index = json.loads(data.decode("utf-8"))
        print(f"Index loaded from R2: {len(_index)} facturas.")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("NoSuchKey", "404"):
            _index = {}
        else:
            print(f"R2 error loading index: {e}")
            _index = {}
    except Exception as exc:
        print(f"Could not load index: {exc}")
        _index = {}


def _refresh_index_from_r2():
    """Reload index from R2 after worker completes."""
    global _index
    try:
        data = _r2_get_bytes("index.json")
        new  = json.loads(data.decode("utf-8"))
        with _index_lock:
            _index = new
        print(f"Index refreshed: {len(_index)} facturas.")
    except Exception as exc:
        print(f"Could not refresh index: {exc}")


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
# Background worker launcher (subprocess — no GIL contention)
# ---------------------------------------------------------------------------
def _launch_worker(pdf_path: Path, fname: str):
    """Spawn worker.py as a child process. Parent stays responsive."""
    status_file = str(BASE_DIR / f".status_{fname}.json")
    _processing[fname] = {
        "status": "processing",
        "pages_done": 0,
        "total": 0,
        "error": None,
        "status_file": status_file,
    }

    worker_script = str(BASE_DIR / "worker.py")
    proc = subprocess.Popen(
        [sys.executable, worker_script, str(pdf_path), fname, status_file],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
    )

    def _monitor():
        stdout, stderr = proc.communicate()
        if proc.returncode == 0:
            _processing[fname]["status"] = "done"
            _refresh_index_from_r2()
        else:
            _processing[fname]["status"] = "error"
            _processing[fname]["error"] = stderr.decode(errors="replace")
        try:
            Path(status_file).unlink(missing_ok=True)
        except Exception:
            pass

    threading.Thread(target=_monitor, daemon=True).start()


def _read_processing_status() -> dict:
    """Return a copy of _processing with live progress from status files."""
    result = {}
    for fname, info in list(_processing.items()):
        entry = dict(info)
        sf = entry.pop("status_file", None)
        if sf and entry.get("status") == "processing":
            try:
                with open(sf, encoding="utf-8") as f:
                    live = json.load(f)
                entry.update({k: v for k, v in live.items() if k != "status_file"})
            except Exception:
                pass
        result[fname] = entry
    return result


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
    """Render single page on demand, cache in R2."""
    ref = request.args.get("ref", "")
    stored_ref, entry = _lookup(ref)
    if not entry:
        return "No encontrado", 404

    img_key = f"images/{stored_ref}.jpg"
    if _r2_exists(img_key):
        return redirect(_pub(img_key))

    try:
        pdf_bytes  = _r2_get_bytes(f"pdfs/{entry['file']}")
        doc        = fitz.open(stream=pdf_bytes, filetype="pdf")
        fitz_page  = doc[entry["page"]]
        mat        = fitz.Matrix(120 / 72, 120 / 72)
        pix        = fitz_page.get_pixmap(matrix=mat, alpha=False)
        jpeg_bytes = pix.tobytes("jpeg", jpg_quality=82)
        doc.close()
        _r2_put(img_key, jpeg_bytes, "image/jpeg")
        return redirect(_pub(img_key))
    except Exception as exc:
        return f"Error al generar imagen: {exc}", 500


# ---------------------------------------------------------------------------
# Routes — Admin
# ---------------------------------------------------------------------------
@app.route("/admin")
def admin():
    with _index_lock:
        total = len(_index)
        files: dict[str, int] = {}
        for entry in _index.values():
            fn = entry["file"]
            files[fn] = files.get(fn, 0) + 1
    return render_template("admin.html",
                           total=total,
                           files=files,
                           processing=_read_processing_status())


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
        dest  = PDF_DIR / fname
        f.save(dest)
        _launch_worker(dest, fname)
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
            fn = entry["file"]
            files[fn] = files.get(fn, 0) + 1
    return jsonify({
        "total": total,
        "files": files,
        "processing": _read_processing_status(),
    })


@app.route("/admin/delete", methods=["POST"])
def admin_delete():
    fname = request.form.get("file", "")
    if not fname:
        flash("Archivo no especificado.", "error")
        return redirect(url_for("admin"))
    removed = 0
    with _index_lock:
        keys = [k for k, v in _index.items() if v.get("file") == fname]
        for k in keys:
            del _index[k]
            removed += 1
    try:
        _r2_put("index.json",
                json.dumps(_index, ensure_ascii=False).encode(),
                "application/json")
    except Exception as exc:
        flash(f"Error actualizando índice en R2: {exc}", "error")
        return redirect(url_for("admin"))
    flash(f"'{fname}' eliminado — {removed} facturas removidas.", "ok")
    return redirect(url_for("admin"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(debug=False, port=port, host="0.0.0.0")
