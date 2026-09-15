"""
Ejecutar localmente: python procesar.py
Pone los PDF en la carpeta  pdfs_entrada/
Genera la carpeta           dist/  lista para subir a Cloudflare Pages
"""
import json
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import fitz
import pdfplumber

# ── Config ────────────────────────────────────────────────────────────────
PDF_FOLDER   = Path(__file__).parent / "pdfs_entrada"
OUTPUT_DIR   = Path(__file__).parent / "dist"
IMG_DIR      = OUTPUT_DIR / "images"
DPI          = 150          # calidad de imagen (sube a 180 si querés más nitidez)
JPEG_QUALITY = 88           # 0-100

CATASTRAL_RE  = re.compile(r"\b(\d{15})\b")
LONG_DIGIT_RE = re.compile(r"^\d{9,}$")

# ── Helpers ───────────────────────────────────────────────────────────────
def catastral_desde_palabras(page) -> str | None:
    umbral = page.height * 0.40
    candidatos = []
    for w in page.extract_words():
        txt = w["text"]
        if LONG_DIGIT_RE.match(txt) and w["top"] < umbral and len(txt) >= 12:
            candidatos.append((len(txt), txt))
    if not candidatos:
        return None
    candidatos.sort(key=lambda x: -x[0])
    return candidatos[0][1]

# ── Main ──────────────────────────────────────────────────────────────────
def main():
    PDF_FOLDER.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)
    IMG_DIR.mkdir(exist_ok=True)

    pdfs = sorted(PDF_FOLDER.glob("*.pdf"))
    if not pdfs:
        print(f"No hay PDFs en '{PDF_FOLDER}'. Poné ahí tus archivos y volvé a correr.")
        return

    # Carga índice existente para no re-procesar lo que ya está
    index_path = OUTPUT_DIR / "index.json"
    index: dict = {}
    if index_path.exists():
        with open(index_path, encoding="utf-8") as f:
            index = json.load(f)

    img_counter = len(list(IMG_DIR.glob("*.jpg")))
    total_nuevas = 0

    for pdf_path in pdfs:
        print(f"\n📄 {pdf_path.name}")
        doc_fitz = fitz.open(pdf_path)

        with pdfplumber.open(pdf_path) as pdf:
            for i, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                matches = CATASTRAL_RE.findall(text)
                ref = matches[0] if matches else catastral_desde_palabras(page)

                if not ref:
                    print(f"  ⚠  Página {i+1}: sin referencia catastral, se omite")
                    continue

                if ref in index:
                    print(f"  ✓  Página {i+1}: {ref} ya indexada")
                    continue

                # Renderizar página como JPEG
                img_counter += 1
                img_name = f"p{img_counter:05d}.jpg"
                fitz_page = doc_fitz[i]
                mat = fitz.Matrix(DPI / 72, DPI / 72)
                pix = fitz_page.get_pixmap(matrix=mat, alpha=False)
                pix.save(str(IMG_DIR / img_name), jpg_quality=JPEG_QUALITY)

                index[ref] = f"images/{img_name}"
                total_nuevas += 1
                print(f"  ✅ Página {i+1}: {ref} → {img_name}")

        doc_fitz.close()

    # Guardar índice
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    print(f"\n{'─'*50}")
    print(f"Facturas nuevas procesadas : {total_nuevas}")
    print(f"Total en el índice         : {len(index)}")
    print(f"Carpeta lista para subir   : dist/")
    print(f"{'─'*50}")

if __name__ == "__main__":
    main()
