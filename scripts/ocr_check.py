#!/usr/bin/env python3
"""OCR check on the GPU host (real Docker and OCR image; no model needed).

    .venv/bin/python scripts/ocr_check.py                    # accuracy on a simulated scan of a demo PDF
    .venv/bin/python scripts/ocr_check.py --report a.pdf ... # pages read by OCR in real documents, no text shown

Accuracy: every page of a PDF with a text layer (default: the demo company's price list) is rendered at 200 dpi
into an image-only PDF — a scan without text — which then goes through Hapilot's extraction (src/extractors.py →
src/ocr.py). The OCR text is compared with the original text layer: character similarity (difflib ratio, whitespace
and case ignored) and the share of Polish diacritics recovered. Fails (exit 1) below ``--min-similarity``.

Report: for each document, the pages without a text layer and, for those, the number of characters OCR recognised
and Tesseract's mean confidence — numbers only, so it can be run on private documents.
"""

from __future__ import annotations

import argparse
import difflib
import io
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DIACRITICS = "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ"


def image_only_pdf(path: Path, dpi: int = 200) -> bytes:
    import pypdfium2 as pdfium

    src, out = pdfium.PdfDocument(str(path)), pdfium.PdfDocument.new()
    for i in range(len(src)):
        page = src[i]
        width, height = page.get_size()
        bitmap = page.render(scale=dpi / 72, grayscale=True)
        image = pdfium.PdfImage.new(out)
        image.set_bitmap(bitmap)
        image.set_matrix(pdfium.PdfMatrix().scale(width, height))
        new = out.new_page(width, height)
        new.insert_obj(image)
        new.gen_content()
    buf = io.BytesIO()
    out.save(buf)
    return buf.getvalue()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def accuracy(path: Path, min_similarity: float) -> int:
    from src.extractors import extract, extract_bytes

    original = {s.meta["page"]: s.text for s in extract(path)}
    scanned = extract_bytes(path.name, image_only_pdf(path))
    pages = []
    for seg in scanned:
        ref = original.get(seg.meta["page"], "")
        ref_d, ocr_d = sum(ref.count(c) for c in DIACRITICS), sum(seg.text.count(c) for c in DIACRITICS)
        pages.append({"page": seg.meta["page"], "ocr": seg.meta.get("ocr", False),
                      "confidence": seg.meta.get("ocr_confidence"), "chars_original": len(ref),
                      "chars_ocr": len(seg.text),
                      "similarity": round(difflib.SequenceMatcher(None, _norm(ref), _norm(seg.text)).ratio(), 3),
                      "diacritics_recovered": round(min(ocr_d, ref_d) / ref_d, 3) if ref_d else None})
    worst = min((p["similarity"] for p in pages), default=0.0)
    passed = bool(pages) and all(p["ocr"] for p in pages) and worst >= min_similarity
    print(json.dumps({"document": path.name, "pages": pages, "min_similarity": min_similarity, "passed": passed},
                     ensure_ascii=False, indent=2))
    return 0 if passed else 1


def report(paths: list[Path]) -> int:
    from src.extractors import ExtractionError, extract

    rows = []
    for n, path in enumerate(paths, 1):
        try:
            segments = extract(path)
        except ExtractionError as exc:
            rows.append({"document": f"doc{n:02d}", "error": str(exc).split(": ", 1)[-1][:120]})
            continue
        ocr = [s for s in segments if s.meta.get("ocr")]
        rows.append({"document": f"doc{n:02d}", "pages_with_text": len(segments), "pages_ocr": len(ocr),
                     "chars_ocr": sum(len(s.text) for s in ocr),
                     "mean_confidence": round(sum(s.meta["ocr_confidence"] for s in ocr) / len(ocr), 1) if ocr else None})
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pdf", type=Path, default=ROOT / "examples" / "demo_company" / "docs" / "cennik_uslug_2026.pdf",
                        help="PDF with a text layer to simulate a scan of")
    parser.add_argument("--min-similarity", type=float, default=0.9)
    parser.add_argument("--report", type=Path, nargs="+", help="documents to report on (numbers only)")
    args = parser.parse_args()

    import os

    from src.config import reset_settings

    os.environ["HAPILOT_OCR"] = "on"
    if not args.report:  # the accuracy check must not answer from an earlier run's cache
        os.environ["HAPILOT_DATA_DIR"] = tempfile.mkdtemp(prefix="hapilot-ocr-check-")
    reset_settings()
    return report(args.report) if args.report else accuracy(args.pdf, args.min_similarity)


if __name__ == "__main__":
    raise SystemExit(main())
