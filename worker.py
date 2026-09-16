"""
PDF processor — runs as a subprocess so it doesn't block the Flask process.
Usage: python worker.py <pdf_path> <fname> <status_file>
R2 credentials come from environment variables (inherited from parent).
"""
import json
import os
import re
import sys
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import fitz  # PyMuPDF — much lighter RAM footprint than pdfplumber

CATASTRAL_RE  = re.compile(r"\b(\d{15})\b")
LONG_DIGIT_RE = re.compile(r"^\d{9,}$")

R2_ENDPOINT_URL      = os.environ.get("R2_ENDPOINT_URL", "")
R2_ACCESS_KEY_ID     = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME       = os.environ.get("R2_BUCKET_NAME", "facturas-prediales")
R2_ACCOUNT_ID        = os.environ.get("R2_ACCOUNT_ID", "")

if not R2_ENDPOINT_URL and R2_ACCOUNT_ID:
    R2_ENDPOINT_URL = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"


def _r2():
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT_URL,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def _write_status(status_file: str, data: dict):
    try:
        with open(status_file, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def _extract_ref_from_page(page) -> str | None:
    """Extract catastral reference using fitz text extraction."""
    text = page.get_text("text")
    matches = CATASTRAL_RE.findall(text)
    if matches:
        return matches[0]

    # Fallback: look for long digit sequences in the top 40% of the page
    rect = page.rect
    threshold_y = rect.height * 0.40
    clip = fitz.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + threshold_y)
    top_text = page.get_text("words", clip=clip)

    candidates = []
    for word in top_text:
        txt = word[4]  # word tuple: (x0, y0, x1, y1, "word", block, line, wnum)
        if LONG_DIGIT_RE.match(txt) and len(txt) >= 12:
            candidates.append((len(txt), txt))

    if candidates:
        candidates.sort(key=lambda x: -x[0])
        return candidates[0][1]

    return None


def main():
    if len(sys.argv) < 4:
        print("Usage: worker.py <pdf_path> <fname> <status_file>")
        sys.exit(1)

    pdf_path    = Path(sys.argv[1])
    fname       = sys.argv[2]
    status_file = sys.argv[3]

    _write_status(status_file, {"status": "processing", "pages_done": 0, "total": 0, "error": None})

    try:
        s3 = _r2()

        # 1. Upload original PDF to R2
        print(f"Uploading PDF to R2: pdfs/{fname}")
        pdf_bytes = pdf_path.read_bytes()
        s3.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=f"pdfs/{fname}",
            Body=pdf_bytes,
            ContentType="application/pdf",
        )
        del pdf_bytes  # free memory immediately after upload

        # 2. Text extraction with fitz (low RAM usage)
        local = {}
        doc = fitz.open(str(pdf_path))
        total = doc.page_count
        _write_status(status_file, {"status": "processing", "pages_done": 0, "total": total, "error": None})

        for i in range(total):
            page = doc[i]
            ref  = _extract_ref_from_page(page)
            if ref:
                local[ref] = {"file": fname, "page": i}
            page = None  # release page object

            if i % 5 == 0:
                _write_status(status_file, {
                    "status": "processing",
                    "pages_done": i + 1,
                    "total": total,
                    "error": None,
                })

        doc.close()

        # 3. Load existing index from R2 and merge
        try:
            obj      = s3.get_object(Bucket=R2_BUCKET_NAME, Key="index.json")
            existing = json.loads(obj["Body"].read().decode("utf-8"))
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                existing = {}
            else:
                raise

        existing.update(local)
        s3.put_object(
            Bucket=R2_BUCKET_NAME,
            Key="index.json",
            Body=json.dumps(existing, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )

        _write_status(status_file, {
            "status": "done",
            "pages_done": total,
            "total": total,
            "indexed": len(local),
            "error": None,
        })
        print(f"Done: {len(local)} facturas indexed from {fname}")

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        _write_status(status_file, {"status": "error", "error": str(exc), "pages_done": 0, "total": 0})
        sys.exit(1)
    finally:
        try:
            pdf_path.unlink(missing_ok=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
