"""OCR for scanned PDF pages, so that scans become searchable like any other document.

A PDF page whose text layer is (almost) empty is treated as a scan (``needs_ocr``). Such pages are rendered at
300 dpi in greyscale (pypdfium2) and read by Tesseract (default languages Polish + English) in a fresh container
of the OCR image — built once by ``scripts/hapilotctl.py setup`` from ``sandbox/ocr.Dockerfile`` — with the
sandbox's isolation: no network, non-root, read-only root filesystem, only a temporary folder with the page images
mounted (src/sandbox.py). Results are cached per document and page in ``<data dir>/ocr_cache/``, so indexing the
same scan again, or showing it in the side panel, does not repeat the OCR.

``HAPILOT_OCR=off`` switches OCR off; without Docker or the OCR image, scanned pages stay unread and indexing says
so. The text is Tesseract's: scans of poor quality give recognition errors; each page keeps Tesseract's mean word
confidence (0–100).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from src.config import get_settings

DPI = 300
MIN_TEXT_CHARS = 25   # a page with fewer non-space characters in its text layer is treated as a scan
VERSION = 1           # bump when rendering or reading changes: cached results and the index are then renewed


class OCRUnavailable(RuntimeError):
    """OCR is off, or Docker / the OCR image is not available."""


@dataclass
class PageText:
    text: str
    confidence: float  # Tesseract's mean word confidence, 0–100 (0 when no word was recognised)


def needs_ocr(text: str) -> bool:
    return len(re.sub(r"\s+", "", text or "")) < MIN_TEXT_CHARS


def signature() -> str:
    """What the OCR would do right now — part of the knowledge base fingerprint, so that the index is rebuilt when
    OCR is switched on, becomes available or changes."""
    s = get_settings()
    if not s.ocr_enabled:
        return "off"
    try:
        _check_image()
    except OCRUnavailable:
        return "unavailable"
    return f"{s.ocr_image} {s.ocr_languages} {DPI}dpi v{VERSION}"


def _check_image() -> None:
    from src import sandbox

    s = get_settings()
    try:
        sandbox.docker_client().images.get(s.ocr_image)
    except sandbox.SandboxUnavailable as exc:
        raise OCRUnavailable(str(exc)) from exc
    except Exception as exc:
        raise OCRUnavailable(f"OCR image {s.ocr_image!r} not found — build it with `scripts/hapilotctl.py setup`") from exc


def _cache_dir(data: bytes) -> Path:
    s = get_settings()
    key = hashlib.sha256(data).hexdigest() + f"|{s.ocr_image}|{s.ocr_languages}|{DPI}|{VERSION}"
    return s.data_dir / "ocr_cache" / hashlib.sha256(key.encode()).hexdigest()


def read_pages(data: bytes, pages: list[int]) -> dict[int, PageText]:
    """Text of the given pages (1-based) of a PDF, from the cache or from Tesseract.

    Raises ``OCRUnavailable`` when OCR is off or cannot run."""
    s = get_settings()
    if not s.ocr_enabled:
        raise OCRUnavailable("OCR is off (HAPILOT_OCR=off)")
    cache = _cache_dir(data)
    result, todo = {}, []
    for page in pages:
        cached = cache / f"{page}.json"
        if cached.is_file():
            saved = json.loads(cached.read_text(encoding="utf-8"))
            result[page] = PageText(saved["text"], saved["confidence"])
        else:
            todo.append(page)
    if todo:
        fresh = _recognise(data, todo)
        cache.mkdir(parents=True, exist_ok=True)
        for page, page_text in fresh.items():
            (cache / f"{page}.json").write_text(json.dumps({"text": page_text.text, "confidence": page_text.confidence},
                                                           ensure_ascii=False), encoding="utf-8")
        result.update(fresh)
    return result


def render_page(pdf, page_no: int) -> bytes:
    """One page of an open pypdfium2 document as a greyscale PNG at ``DPI``."""
    from src.source_view import png

    page = pdf[page_no - 1]
    try:
        bitmap = page.render(scale=DPI / 72, grayscale=True)
        try:
            return png(bytes(bitmap.buffer), bitmap.width, bitmap.height, bitmap.stride, bitmap.n_channels)
        finally:
            bitmap.close()
    finally:
        page.close()


def _recognise(data: bytes, pages: list[int]) -> dict[int, PageText]:
    import pypdfium2 as pdfium

    from src import sandbox

    s = get_settings()
    _check_image()
    tmp_root = s.data_dir / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="ocr-", dir=tmp_root))
    try:
        pdf = pdfium.PdfDocument(data)
        try:
            for page in pages:
                (work / f"p{page:05d}.png").write_bytes(render_page(pdf, page))
        finally:
            pdf.close()
        # one container for the whole document; languages are checked in src/config.py ([a-z_+] only)
        script = (f'for f in /work/p*.png; do tesseract "$f" "${{f%.png}}" -l {s.ocr_languages} --dpi {DPI} '
                  f'txt tsv 2>/dev/null || {{ echo "tesseract failed on $f"; exit 1; }}; done')
        try:
            result = sandbox.run(["sh", "-c", script], work, timeout=60 + 60 * len(pages), image=s.ocr_image)
        except sandbox.SandboxUnavailable as exc:
            raise OCRUnavailable(str(exc)) from exc
        if not result.ok:
            raise OCRUnavailable("OCR " + ("timed out" if result.timed_out else f"failed (exit {result.exit_code})")
                                 + (f": {result.output[-300:]}" if result.output else ""))
        texts = {}
        for page in pages:
            txt, tsv = work / f"p{page:05d}.txt", work / f"p{page:05d}.tsv"
            text = txt.read_text(encoding="utf-8", errors="replace") if txt.is_file() else ""
            texts[page] = PageText(_clean(text), _confidence(tsv.read_text(encoding="utf-8", errors="replace")
                                                                if tsv.is_file() else ""))
        return texts
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _clean(text: str) -> str:
    text = text.replace("\f", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _confidence(tsv: str) -> float:
    """Mean confidence of the recognised words in Tesseract's TSV output."""
    values = []
    for line in tsv.splitlines()[1:]:
        cols = line.split("\t")
        if len(cols) == 12 and cols[0] == "5" and cols[11].strip():
            try:
                conf = float(cols[10])
            except ValueError:
                continue
            if conf >= 0:
                values.append(conf)
    return round(sum(values) / len(values), 1) if values else 0.0
