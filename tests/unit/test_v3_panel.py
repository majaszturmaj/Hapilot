"""Engine v3 and the side panel "📎 Źródła i pliki": the documents behind an answer and the files the agent made.

Found on the pilot (2026-11-23): under v3 the panel never opened — an agent's PDF letter came only as a download
under the answer, and the documents it read were not shown, unlike under v2."""

import json
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from src import agent_engine, text_mirror, ui_panel
from src.agent_engine import opened_paths
from src.config import get_settings, reset_settings
from src.source_view import ATTACHMENT_PREFIX


def _pdf(path: Path, pages: int = 2) -> Path:
    doc = pdfium.PdfDocument.new()
    for _ in range(pages):
        doc.new_page(595, 842)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


# --------------------------------------------------------------------------- what the agent opened

def test_read_files_paths_are_documents_and_text_copies_only():
    raw = json.dumps({"paths": ["/praca/tekst/umowy/umowa ramowa.pdf.txt", "/praca/dokumenty/cennik.xlsx",
                                "/praca/wyniki/pismo.pdf", "/etc/passwd"]})
    assert opened_paths("read_files", raw) == ["/praca/tekst/umowy/umowa ramowa.pdf.txt", "/praca/dokumenty/cennik.xlsx"]


def test_command_paths_quoted_bare_and_in_python():
    raw = json.dumps({"commands": [
        "grep -ril faktura /praca/tekst | head",                       # a folder: not a document
        'cat "/praca/tekst/Oferty/oferta 12.docx.txt"',
        "python3 -c \"import pandas as pd; pd.read_csv('/praca/tekst/dane.xlsx.Arkusz1.csv')\"",
        "sed -n 1,40p /praca/dokumenty/regulamin.pdf;",
        "ls /praca/zalaczniki/",
    ]})
    assert opened_paths("run_commands", raw) == ["/praca/tekst/Oferty/oferta 12.docx.txt",
                                                 "/praca/tekst/dane.xlsx.Arkusz1.csv", "/praca/dokumenty/regulamin.pdf"]


def test_other_tools_and_bad_input_open_nothing():
    assert opened_paths("editor", json.dumps({"path": "/praca/dokumenty/a.pdf"})) == []
    assert opened_paths("semantic_search", json.dumps({"query": "x"})) == []
    assert opened_paths("read_files", "not json /praca/dokumenty/a.pdf") == []
    assert opened_paths("run_commands", "") == []


def test_changed_results_say_created_or_modified(tmp_path):
    class Ws:
        def __init__(self):
            self.files = {}

        def results(self):
            return [{"name": n, "path": p, "bytes": p.stat().st_size} for n, p in sorted(self.files.items())]

    ws = Ws()
    old = tmp_path / "stary.txt"
    old.write_text("a")
    ws.files["stary.txt"] = old
    before = agent_engine._snapshot(ws)
    old.write_text("ab")
    new = tmp_path / "pismo.pdf"
    new.write_bytes(b"%PDF")
    ws.files["pismo.pdf"] = new
    assert {f["name"]: f["status"] for f in agent_engine._changed(ws, before)} == {"pismo.pdf": "created",
                                                                                  "stary.txt": "modified"}


# --------------------------------------------------------------------------- container path → document

def test_agent_source_maps_every_mount(tmp_path):
    uploads = tmp_path / "zalaczniki"
    uploads.mkdir()
    (uploads / "faktura.pdf").write_bytes(b"x")
    (uploads / "faktura.pdf.txt").write_text("x")
    (uploads / "dane.xlsx").write_bytes(b"x")
    (uploads / "notatka.txt").write_text("x")
    mirror = {"umowy/umowa.pdf.txt": "umowy/umowa.pdf", "dodane/x.docx.txt": "dodane/x.docx"}
    src = ui_panel.agent_source
    assert src("/praca/dokumenty/umowy/umowa.pdf", mirror, uploads) == "umowy/umowa.pdf"
    assert src("/praca/dodane/x.docx", mirror, uploads) == "dodane/x.docx"
    assert src("/praca/tekst/umowy/umowa.pdf.txt", mirror, uploads) == "umowy/umowa.pdf"
    assert src("/praca/tekst/nieznany.txt", mirror, uploads) is None
    assert src("/praca/zalaczniki/faktura.pdf.txt", mirror, uploads) == ATTACHMENT_PREFIX + "faktura.pdf"
    assert src("/praca/zalaczniki/dane.xlsx.Arkusz1.csv", mirror, uploads) == ATTACHMENT_PREFIX + "dane.xlsx"
    assert src("/praca/zalaczniki/notatka.txt", mirror, uploads) == ATTACHMENT_PREFIX + "notatka.txt"
    assert src("/praca/zalaczniki/brak.pdf", mirror, uploads) is None
    assert src("/praca/wyniki/pismo.pdf", mirror, uploads) is None
    assert src("/praca/dokumenty/", mirror, uploads) is None


# --------------------------------------------------------------------------- the panel turn

@pytest.fixture
def company(tmp_path, monkeypatch):
    data, docs = tmp_path / "var", tmp_path / "docs"
    monkeypatch.setenv("HAPILOT_DATA_DIR", str(data))
    monkeypatch.setenv("HAPILOT_DOCS_DIR", str(docs))
    monkeypatch.delenv("HAPILOT_ADDED_DOCS_DIR", raising=False)
    monkeypatch.delenv("HAPILOT_CHROMA_DIR", raising=False)
    reset_settings()
    s = get_settings()
    _pdf(docs / "umowy" / "umowa.pdf")
    (docs / "cennik.txt").parent.mkdir(parents=True, exist_ok=True)
    (docs / "cennik.txt").write_text("Cena A: 10 zł\nCena B: 20 zł\n", encoding="utf-8")
    (docs / "regulamin.txt").write_text("Reklamacje w 14 dni.\n", encoding="utf-8")
    text_mirror._write_manifest(s.chroma_path, s.collection, {
        "umowy/umowa.pdf": {"outputs": ["umowy/umowa.pdf.txt"]},
        "cennik.txt": {"outputs": ["cennik.txt.txt"]},
        "regulamin.txt": {"outputs": ["regulamin.txt.txt"]},
    })
    yield {"s": s, "docs": docs, "ws": tmp_path / "czat"}
    reset_settings()


def test_turn_shows_read_searched_and_named_documents_and_the_pdf_result(company):
    ws = company["ws"]
    result = _pdf(ws / "praca" / "wyniki" / "pismo.pdf", pages=1)
    (ws / "praca" / "wyniki" / "pismo.tex").write_text("\\documentclass{article}")
    files = [{"name": "pismo.pdf", "path": result, "bytes": result.stat().st_size, "status": "created"},
             {"name": "pismo.tex", "path": ws / "praca" / "wyniki" / "pismo.tex", "bytes": 22, "status": "created"}]
    retrievals = [{"query": "ceny", "k": 8, "ms": 5, "chunks": [
        {"rank": 1, "file": "cennik.txt", "chunk_id": "cennik.txt#0:abc", "score": 0.9, "page": None,
         "text": "Cena B: 20 zł"}]}]
    opened = ["/praca/tekst/umowy/umowa.pdf.txt", "/praca/tekst/nie-ma.txt"]
    turn = ui_panel.build_agent_turn("czat-1", "Napisz pismo do klienta", "Zgodnie z regulamin.txt …", files,
                                     retrievals, opened, ws / "zalaczniki")
    assert turn["question"] == "Napisz pismo do klienta"
    by_source = {d["source"]: d for d in turn["sources"]}
    assert [d["source"] for d in turn["sources"]] == ["regulamin.txt", "umowy/umowa.pdf", "cennik.txt"]
    assert by_source["regulamin.txt"]["cited"] and not by_source["regulamin.txt"]["read_by_agent"]
    assert by_source["umowy/umowa.pdf"]["read_by_agent"] and by_source["umowy/umowa.pdf"]["kind"] == "pdf"
    assert len(by_source["umowy/umowa.pdf"]["pages"]) == 2
    assert by_source["cennik.txt"]["kind"] == "text" and len(by_source["cennik.txt"]["chunks"]) == 1
    assert [f["name"] for f in turn["files"]] == ["pismo.pdf"]  # the .tex next to the PDF is a helper
    pdf = turn["files"][0]
    assert pdf["status"] == "created" and len(pdf["pages"]) == 1
    assert pdf["url"].startswith(ui_panel.ROUTE_PREFIX + "/") and pdf["pages"][0]["url"] != pdf["url"]
    ui_panel.forget_session("czat-1")


def test_turn_with_a_text_result_has_its_preview(company):
    ws = company["ws"]
    note = ws / "praca" / "wyniki" / "notatka.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Notatka\n", encoding="utf-8")
    turn = ui_panel.build_agent_turn("czat-2", "notatka", "Gotowe.", [
        {"name": "notatka.md", "path": note, "bytes": note.stat().st_size, "status": "modified"}], [], [], None)
    assert turn["sources"] == []
    assert turn["files"][0]["preview"] == "# Notatka\n" and turn["files"][0]["status"] == "modified"
    ui_panel.forget_session("czat-2")


def test_no_documents_and_no_files_means_no_panel(company):
    assert ui_panel.build_agent_turn("czat-3", "cześć", "Dzień dobry!", [], [], [], None) is None
