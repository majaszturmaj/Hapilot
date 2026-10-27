"""pdfium is not thread-safe: every use goes through source_view.PDFIUM_LOCK.

Found on the pilot (2026-10-26): the UI process died with a segfault in libpdfium while a browser loaded a panel's
page images in parallel; a 16-thread stress run reproduced it 3 times out of 3 (segfault, "double free")."""

import io
import subprocess
import sys
import textwrap

import pytest

from src import source_view
from src.config import PROJECT_ROOT, reset_settings

DEMO_PDF = PROJECT_ROOT / "examples" / "demo_company" / "docs" / "cennik_uslug_2026.pdf"


def _pdf(tmp_path, pages=6):
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_page(PdfReader(str(DEMO_PDF)).pages[0])
    path = tmp_path / "doc.pdf"
    with open(path, "wb") as fh:
        writer.write(fh)
    return path


@pytest.fixture
def opened(monkeypatch):
    """Records, for every pdfium document opened, whether the opening thread held the lock."""
    import pypdfium2 as pdfium

    seen = []
    original = pdfium.PdfDocument

    class Checked(original):
        def __init__(self, *args, **kwargs):
            seen.append(source_view.PDFIUM_LOCK._is_owned())
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(pdfium, "PdfDocument", Checked)
    return seen


def test_every_side_panel_use_holds_the_lock(tmp_path, opened):
    if not DEMO_PDF.is_file():
        pytest.skip("demo PDF missing")
    path = _pdf(tmp_path, 2)
    source_view.pdf_page_sizes(path)
    source_view.pdf_scanned_pages(path)
    source_view.pdf_boxes(path, 1, [("Cennik", 0)])
    assert source_view.render_pdf_page(path, 2, scale=0.3)[:8] == b"\x89PNG\r\n\x1a\n"
    assert opened == [True] * 4


def test_ocr_rendering_holds_the_lock(tmp_path, monkeypatch, docker, opened):
    from pypdf import PdfWriter

    from src.extractors import extract_bytes

    monkeypatch.setenv("HAPILOT_OCR", "on")
    monkeypatch.setenv("HAPILOT_DATA_DIR", str(tmp_path / "var"))
    reset_settings()
    writer = PdfWriter()
    for _ in range(2):
        writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    assert len(extract_bytes("skan.pdf", buf.getvalue())) == 2
    assert opened and all(opened)


def test_many_threads_at_once_do_not_crash(tmp_path):
    """Run in a separate process: without the lock pdfium kills the process instead of failing a test."""
    if not DEMO_PDF.is_file():
        pytest.skip("demo PDF missing")
    path = _pdf(tmp_path)
    script = textwrap.dedent(f"""
        import sys, threading, time
        sys.path.insert(0, {str(PROJECT_ROOT)!r})
        from pathlib import Path
        from src import source_view
        path, stop, errors = Path({str(path)!r}), time.time() + 3, []
        def work(i):
            while time.time() < stop:
                try:
                    source_view.render_pdf_page(path, i % 6 + 1, scale=0.4)
                    source_view.pdf_boxes(path, i % 6 + 1, [("Cennik", 0)])
                    source_view.pdf_scanned_pages(path)
                except Exception as exc:
                    errors.append(repr(exc))
        threads = [threading.Thread(target=work, args=(i,)) for i in range(12)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        print("errors", len(errors), errors[:2])
    """)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"process died (exit {proc.returncode}): {proc.stderr[-300:]}"
    assert "errors 0 " in proc.stdout, proc.stdout
