"""Side panel data: chunk matching, PDF highlight boxes, documents behind an answer, workspace changes."""

import struct
import zlib

import pytest

from src import run_context, source_view, tracing
from src.config import PROJECT_ROOT
from src.extractors import Segment, extract
from src.ingest_data import split_entries

DOCS = PROJECT_ROOT / "examples" / "demo_company" / "docs"


def _chunks(name: str) -> list[dict]:
    """The demo document cut into chunks exactly as the index cuts it."""
    docs, skipped = split_entries([(DOCS / name, name)])
    assert not skipped
    return [{"file": name, "chunk_id": d.metadata["chunk_id"], "text": d.page_content, "page": d.metadata.get("page"),
             "sheet": d.metadata.get("sheet"), "collection": "corpus", "score": 0.1, "rank": i + 1}
            for i, d in enumerate(docs)]


# --------------------------------------------------------------------------- matching

def test_find_ranges_ignores_whitespace_and_case():
    hay = "Wniosek urlopowy\nskłada się   co najmniej 14 dni przed urlopem."
    [(start, end)] = source_view.find_ranges(hay, "wniosek urlopowy składa się co najmniej 14 dni")
    assert hay[start:end] == "Wniosek urlopowy\nskłada się   co najmniej 14 dni"


def test_find_ranges_falls_back_to_lines():
    hay = "Pierwsze zdanie jest tutaj. Coś innego w środku. Drugie zdanie również jest tutaj."
    ranges = source_view.find_ranges(hay, "Pierwsze zdanie jest tutaj.\nTEGO NIE MA W TEKŚCIE ŻADNYM\nDrugie zdanie również jest tutaj.")
    assert [hay[s:e] for s, e in ranges] == ["Pierwsze zdanie jest tutaj.", "Drugie zdanie również jest tutaj."]


def test_find_ranges_no_match():
    assert source_view.find_ranges("abc", "zupełnie inny tekst niż w dokumencie") == []


def test_spreadsheet_header_is_not_part_of_the_document_text():
    assert source_view.chunk_body("[kolumny: A | B]\n1 | 2") == "1 | 2"


@pytest.mark.parametrize("name", ["regulamin_urlopow.txt", "umowa_serwisowa_7_2026.docx", "rejestr_reklamacji_2026.xlsx",
                                  "zgloszenie_awarii_2026-10-12.eml"])
def test_every_indexed_chunk_is_marked_in_its_document(name):
    chunks = _chunks(name)
    sections = source_view.text_marks(extract(DOCS / name), chunks)
    for no, chunk in enumerate(chunks):
        marked = "".join(s["text"][a:b] for s in sections for a, b, n in s["marks"] if n == no)
        assert source_view._norm(marked)[0] == source_view._norm(source_view.chunk_body(chunk["text"]))[0], no


def test_chunk_is_looked_up_on_its_own_sheet_first():
    segments = [Segment("same row text here", {"sheet": "A"}), Segment("same row text here", {"sheet": "B"})]
    sections = source_view.text_marks(segments, [{"text": "same row text here", "sheet": "B"}])
    assert [bool(s["marks"]) for s in sections] == [False, True]


# --------------------------------------------------------------------------- PDF

def test_pdf_chunks_get_boxes_on_their_page():
    path = DOCS / "cennik_uslug_2026.pdf"
    chunks = _chunks("cennik_uslug_2026.pdf")
    boxes = source_view.pdf_boxes(path, 1, [(source_view.chunk_body(c["text"]), i) for i, c in enumerate(chunks)])
    assert {b["chunk"] for b in boxes} == set(range(len(chunks)))
    for b in boxes:
        assert 0 <= b["x"] < 1 and 0 <= b["y"] < 1 and 0 < b["w"] <= 1 and 0 < b["h"] < 1
    assert min(b["y"] for b in boxes) < 0.2  # the title sits at the top of the page


def test_every_chunk_of_a_ten_page_pdf_is_covered_on_its_page():
    """pypdf (index) and pdfium (boxes) read the same text; each chunk is found whole, ignoring whitespace."""
    import pypdfium2 as pdfium

    path = PROJECT_ROOT / "examples" / "demo_company" / "uploads" / "raport_kwartalny_q3_2026.pdf"
    docs, _ = split_entries([(path, path.name)])
    pdf = pdfium.PdfDocument(str(path))
    assert len(pdf) == 10 and len(docs) > 20
    for d in docs:
        textpage = pdf[d.metadata["page"] - 1].get_textpage()
        page_text = textpage.get_text_range(0, textpage.count_chars())
        body = source_view.chunk_body(d.page_content)
        ranges = source_view.find_ranges(page_text, body)
        covered = sum(len(source_view._norm(page_text[a:b])[0]) for a, b in ranges)
        assert covered >= 0.98 * len(source_view._norm(body)[0]), d.metadata["chunk_id"]
    pdf.close()


def test_pdf_page_renders_as_png():
    data = source_view.render_pdf_page(DOCS / "cennik_uslug_2026.pdf", 1, scale=0.5)
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", data[16:24])
    assert (width, height) == (306, 396)


def test_png_encoder_round_trip():
    pixels = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 9, 9, 9])  # 2x2 RGB
    data = source_view.png(pixels, 2, 2, 6, 3)
    idat = data[data.index(b"IDAT") + 4:data.index(b"IEND") - 8]
    assert zlib.decompress(idat) == b"\x00" + pixels[:6] + b"\x00" + pixels[6:]


# --------------------------------------------------------------------------- documents behind an answer

def test_resolve_source_stays_inside_its_directory(tmp_path):
    docs, added, ws = tmp_path / "docs", tmp_path / "added", tmp_path / "ws"
    for d in (docs, added, ws):
        d.mkdir()
    (docs / "a.txt").write_text("x")
    (added / "b.txt").write_text("y")
    (ws / "c.txt").write_text("z")
    (tmp_path / "secret.txt").write_text("s")
    assert source_view.resolve_source("a.txt", docs, added, ws) == (docs / "a.txt").resolve()
    assert source_view.resolve_source("dodane/b.txt", docs, added, ws) == (added / "b.txt").resolve()
    assert source_view.resolve_source("załącznik:c.txt", docs, added, ws) == (ws / "c.txt").resolve()
    assert source_view.resolve_source("../secret.txt", docs, added, ws) is None
    assert source_view.resolve_source("nie_ma.txt", docs, added, ws) is None


def test_source_docs_cited_first_and_described():
    observer = source_view.RunObserver()
    chunks = _chunks("regulamin_urlopow.txt")[:1] + _chunks("cennik_uslug_2026.pdf")[:1]
    observer("retrieval", {"chunks": chunks})
    observer("retrieval", {"chunks": chunks})  # the same chunks again are kept once
    answer = "Ceny podaje dokument cennik_uslug_2026.pdf."
    docs = source_view.source_docs(observer, answer, DOCS, DOCS / "nie_ma", None)
    assert [(d.source, d.cited, len(d.chunks)) for d in docs] == [
        ("cennik_uslug_2026.pdf", True, 1), ("regulamin_urlopow.txt", False, 1)]
    pdf = docs[0].describe(lambda path, n: f"/page/{path.name}/{n}")
    assert pdf["kind"] == "pdf" and pdf["pages"][0]["url"] == "/page/cennik_uslug_2026.pdf/1"
    assert pdf["pages"][0]["boxes"] and pdf["pages"][0]["ratio"] == pytest.approx(792 / 612, abs=1e-3)
    txt = docs[1].describe(lambda path, n: "")
    assert txt["kind"] == "text" and txt["sections"][0]["marks"]


def test_missing_document_still_shows_its_chunks():
    observer = source_view.RunObserver()
    observer("retrieval", {"chunks": [{"file": "usuniety.txt", "chunk_id": "x#0:1", "text": "Fragment tekstu."}]})
    [doc] = source_view.source_docs(observer, "", DOCS, DOCS, None)
    described = doc.describe(lambda p, n: "")
    assert described["kind"] == "missing" and described["chunks"][0]["text"] == "Fragment tekstu."


def test_files_read_by_code_are_sources_without_chunks():
    observer = source_view.RunObserver()
    observer("code_data_access", {"files": ["rejestr_reklamacji_2026.xlsx"]})
    [doc] = source_view.source_docs(observer, "", DOCS, DOCS, None)
    assert doc.read_by_code and not doc.chunks and doc.describe(lambda p, n: "")["kind"] == "table"


def test_tracing_events_reach_the_run_observer():
    seen = []
    with run_context.use_context(observer=lambda t, f: seen.append((t, f))):
        tracing.event("retrieval", chunks=[{"file": "a.txt"}])
    tracing.event("retrieval", chunks=[])  # no observer: nothing happens
    assert seen == [("retrieval", {"chunks": [{"file": "a.txt"}]})]


def test_a_failing_observer_does_not_break_the_run():
    def broken(t, f):
        raise RuntimeError("panel bug")

    with run_context.use_context(observer=broken):
        tracing.event("retrieval", chunks=[])


# --------------------------------------------------------------------------- workspace changes

def test_snapshot_diff_created_and_modified(tmp_path):
    (tmp_path / "notatka.md").write_text("linia 1\nlinia 2\n", encoding="utf-8")
    (tmp_path / "stary.pdf").write_bytes(b"%PDF-1")
    (tmp_path / "snippet_1.py").write_text("print(1)")
    before = source_view.snapshot(tmp_path)
    assert "snippet_1.py" not in before
    (tmp_path / "notatka.md").write_text("linia 1\nlinia 2 poprawiona\n", encoding="utf-8")
    (tmp_path / "raport.txt").write_text("nowy", encoding="utf-8")
    (tmp_path / "x.params.json").write_text("{}")
    changes = {c.name: c for c in source_view.changes(before, source_view.snapshot(tmp_path))}
    assert set(changes) == {"notatka.md", "raport.txt"}
    assert changes["raport.txt"].status == "created" and changes["raport.txt"].preview == "nowy"
    assert changes["notatka.md"].status == "modified"
    assert "-linia 2" in changes["notatka.md"].diff and "+linia 2 poprawiona" in changes["notatka.md"].diff


def test_binary_files_have_no_preview(tmp_path):
    (tmp_path / "wykres.png").write_bytes(b"\x89PNG....")
    [change] = source_view.changes({}, source_view.snapshot(tmp_path))
    assert change.preview is None and change.status == "created"


# --------------------------------------------------------------------------- panel turns (src/ui_panel.py)

def test_panel_turn_has_sources_and_files_and_urls_are_tokens():
    from src import ui_panel

    pending = ui_panel.start_turn("panel-test", "Ile kosztuje tłumaczenie strony?")
    pending.observer("retrieval", {"chunks": _chunks("cennik_uslug_2026.pdf")})
    with run_context.use_context(session_id="panel-test"):
        (run_context.workspace_dir() / "odpowiedz.md").write_text("60 zł", encoding="utf-8")
    turn = ui_panel.build_turn("panel-test", pending, "Według cennik_uslug_2026.pdf: 60 zł.")
    assert turn["question"] == "Ile kosztuje tłumaczenie strony?"
    [source] = turn["sources"]
    assert source["cited"] and source["kind"] == "pdf"
    [file] = turn["files"]
    assert file["name"] == "odpowiedz.md" and file["status"] == "created" and file["preview"] == "60 zł"
    token = file["url"].rsplit("/", 1)[1]
    assert ui_panel._tokens[token]["path"].name == "odpowiedz.md" and "odpowiedz" not in token
    turns = ui_panel.add_turn([], turn)
    assert ui_panel.add_turn(turns, dict(turn))[-1]["id"] == 2
    ui_panel.forget_session("panel-test")
    assert token not in ui_panel._tokens


def test_panel_turn_is_empty_without_sources_or_files():
    from src import ui_panel

    pending = ui_panel.start_turn("panel-empty", "Cześć")
    assert ui_panel.build_turn("panel-empty", pending, "Cześć!") is None
