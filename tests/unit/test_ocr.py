"""OCR of scanned PDF pages: which pages, how the container runs, cache, failures, indexing and the side panel."""

import io
import json

import pytest

from src import ingest_data, ocr
from src.config import PROJECT_ROOT, ConfigError, load_settings, reset_settings
from src.extractors import ExtractionError, extract_bytes

DEMO_PDF = PROJECT_ROOT / "examples" / "demo_company" / "docs" / "cennik_uslug_2026.pdf"


def _blank_pdf(pages: int = 1) -> bytes:
    """A PDF whose pages have no text layer, like a scan (the fake OCR does not look at the pixels)."""
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _mixed_pdf() -> bytes:
    """Page 1 with a text layer (the demo price list), page 2 a 'scan'."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.add_page(PdfReader(str(DEMO_PDF)).pages[0])
    writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def ocr_on(monkeypatch, docker, tmp_path):
    monkeypatch.setenv("HAPILOT_OCR", "on")
    monkeypatch.setenv("HAPILOT_DATA_DIR", str(tmp_path / "var"))  # own OCR cache per test
    reset_settings()
    return docker


def test_settings():
    s = load_settings({})
    assert (s.ocr_enabled, s.ocr_image, s.ocr_languages) == (True, "hapilot-ocr:0.1", "pol+eng")
    assert load_settings({"HAPILOT_OCR": "off", "HAPILOT_OCR_LANGUAGES": "pol"}).ocr_enabled is False
    for env in ({"HAPILOT_OCR": "maybe"}, {"HAPILOT_OCR_LANGUAGES": "pol; rm -rf /"}, {"HAPILOT_OCR_LANGUAGES": ""}):
        with pytest.raises(ConfigError):
            load_settings(env)


def test_needs_ocr():
    assert ocr.needs_ocr("") and ocr.needs_ocr(" 3 \n") and ocr.needs_ocr("Strona 3 z 8")
    assert not ocr.needs_ocr("Umowa serwisowa nr 7/2026 zawarta w dniu 1 października")


def test_scanned_pdf_is_read_in_the_isolated_ocr_container(ocr_on):
    segments = extract_bytes("skan.pdf", _blank_pdf(2))
    assert [s.meta["page"] for s in segments] == [1, 2]
    assert segments[0].text == ocr_on.ocr_text and segments[0].meta["ocr"] is True
    assert segments[0].meta["ocr_confidence"] == pytest.approx(85.0, abs=1)  # mean of the fake TSV's word confidences
    [run] = ocr_on.runs  # one container for the whole document
    assert run["image"] == "hapilot-ocr:0.1" and run["network_mode"] == "none" and run["read_only"] is True
    assert run["cap_drop"] == ["ALL"] and "-l pol+eng" in run["command"][2] and "--dpi 300" in run["command"][2]
    assert ocr_on.ocr_pages == ["p00001.png", "p00002.png"]


def test_only_pages_without_text_are_read_and_results_are_cached(ocr_on):
    if not DEMO_PDF.is_file():
        pytest.skip("demo PDF missing")
    data = _mixed_pdf()
    segments = extract_bytes("mieszany.pdf", data)
    assert "Cennik" in segments[0].text and "ocr" not in segments[0].meta
    assert segments[1].meta["ocr"] is True and ocr_on.ocr_pages == ["p00002.png"]
    again = extract_bytes("mieszany.pdf", data)
    assert len(ocr_on.runs) == 1 and again[1].text == segments[1].text  # second reading from the cache


def test_ocr_off_or_unavailable_is_reported(monkeypatch, docker):
    with pytest.raises(ExtractionError, match="HAPILOT_OCR=off"):  # conftest: OCR off
        extract_bytes("skan.pdf", _blank_pdf())
    assert docker.runs == [] and ocr.signature() == "off"

    from fakes import FakeDocker
    from src import sandbox

    monkeypatch.setenv("HAPILOT_OCR", "on")
    reset_settings()
    sandbox.set_docker_client(FakeDocker(image_available=False))
    with pytest.raises(ExtractionError, match="not found"):
        extract_bytes("skan.pdf", _blank_pdf())
    assert ocr.signature() == "unavailable"


def test_failed_ocr_keeps_the_text_pages(ocr_on, capsys):
    if not DEMO_PDF.is_file():
        pytest.skip("demo PDF missing")
    ocr_on.ocr_fails = True
    segments = extract_bytes("mieszany.pdf", _mixed_pdf())
    assert [s.meta["page"] for s in segments] == [1]
    assert "OCR nie zadziałał" in capsys.readouterr().out
    assert not list(ocr.get_settings().data_dir.glob("ocr_cache/*/*.json"))  # a failure is never cached
    ocr_on.ocr_fails = False
    assert [s.meta["page"] for s in extract_bytes("mieszany.pdf", _mixed_pdf())] == [1, 2]  # tried again next time


def test_confidence_from_tsv():
    tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
           "5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t96.5\tUmowa\n5\t1\t1\t1\t1\t2\t0\t0\t1\t1\t-1\t\n"
           "5\t1\t1\t1\t1\t3\t0\t0\t1\t1\t71.5\tnr\n4\t1\t1\t1\t1\t0\t0\t0\t1\t1\t-1\t\n")
    assert ocr._confidence(tsv) == 84.0 and ocr._confidence("") == 0.0


def test_scanned_chunks_are_marked_and_the_index_follows_ocr(ocr_on, tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "protokol_skan.pdf").write_bytes(_blank_pdf())
    chunks, skipped = ingest_data.split_documents([docs / "protokol_skan.pdf"], docs)
    assert skipped == [] and len(chunks) == 1
    assert chunks[0].metadata == {"source": "protokol_skan.pdf", "page": 1, "ocr": True,
                                  "chunk_id": chunks[0].metadata["chunk_id"]}
    on = ingest_data._signature("x")["ocr"]
    assert on.startswith("hapilot-ocr:0.1 pol+eng 300dpi")
    monkeypatch.setenv("HAPILOT_OCR", "off")
    reset_settings()
    assert ingest_data._signature("x")["ocr"] == "off" != on  # switching OCR changes the fingerprint → re-index


def test_side_panel_marks_scanned_pages(tmp_path):
    if not DEMO_PDF.is_file():
        pytest.skip("demo PDF missing")
    from src.source_view import SourceDoc, pdf_scanned_pages

    path = tmp_path / "mieszany.pdf"
    path.write_bytes(_mixed_pdf())
    assert pdf_scanned_pages(path) == {2}
    doc = SourceDoc("mieszany.pdf", path, [{"page": 2, "text": "Protokół odbioru zeskanowany", "chunk_id": "a:0"}], True)
    pages = doc.describe(lambda p, n: f"/page/{n}")["pages"]
    assert [p["scan"] for p in pages] == [False, True] and pages[1]["boxes"] == []
    json.dumps(pages)  # sent to the browser as JSON
