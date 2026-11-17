"""Text mirror (src/text_mirror.py): rendering per format, and keeping the mirror in step with the index's document
list through ingest, single-document changes, chat additions, removals and ``sync`` (in-memory stand-in for Chroma)."""

import csv
import io
import json
from datetime import date
from email.message import EmailMessage
from pathlib import Path

import pytest

from src import ingest_data, knowledge_admin, knowledge_base, text_mirror
from src.config import get_settings, reset_settings
from src.extractors import CSV_ROWS, Segment, extract, extract_bytes


# --------------------------------------------------------------------------- fixtures built in the test

def _docx(path: Path, first_page: bool = False) -> Path:
    from docx import Document

    doc = Document()
    section = doc.sections[0]
    section.header.paragraphs[0].text = "Przykładowa Sp. z o.o. — dokument wewnętrzny KAD/7"
    section.footer.paragraphs[0].text = "Strona stopki: wersja 3 z 2026-02-01"
    if first_page:
        section.different_first_page_header_footer = True
        section.first_page_header.paragraphs[0].text = "Pierwsza strona: pismo przewodnie"
    doc.add_paragraph("Treść umowy serwisowej.")
    table = doc.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "Usługa", "Cena"
    doc.save(path)
    return path


def _eml(path: Path, plain: str, html: str | None) -> Path:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "biuro@example.com", "kadry@example.com", "Wniosek"
    msg.set_content(plain)
    if html is not None:
        msg.add_alternative(html, subtype="html")
    path.write_bytes(bytes(msg))
    return path


def _xlsx(path: Path) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Faktury"
    ws["B3"], ws["C3"], ws["D3"] = "Nr", "Data", "Kwota"
    ws["B4"], ws["C4"], ws["D4"] = "F/1", date(2026, 3, 2), 1500.0
    ws["B5"], ws["C5"], ws["D5"] = "F/2", date(2026, 3, 9), 320.5
    wb.create_sheet("A|B").append(["x", None, "z"])
    wb.create_sheet("A_B").append(["y"])
    wb.create_sheet("Puste")
    wb.save(path)
    return path


# --------------------------------------------------------------------------- rendering

def test_pdf_pages_are_marked_with_ocr_confidence_and_pages_without_text(tmp_path, monkeypatch):
    pdf = tmp_path / "skan.pdf"
    pdf.write_bytes(b"%PDF placeholder")
    monkeypatch.setattr(text_mirror, "_pdf_pages", lambda data: 3)
    segments = [Segment("Umowa najmu", {"page": 1}),
                Segment("Podpisy stron", {"page": 2, "ocr": True, "ocr_confidence": 87.4})]
    out = text_mirror.render(pdf, segments)
    assert out.files[".txt"] == ("[strona 1]\nUmowa najmu\n\n[strona 2 — OCR, pewność 87%]\nPodpisy stron\n\n"
                                 "[strona 3 — brak tekstu]\n")
    assert (out.pages, out.ocr_pages, out.error) == (3, [2], None)


def test_real_pdf_uses_the_extractors_text(tmp_path):
    pdf = Path(__file__).resolve().parents[2] / "examples" / "demo_company" / "docs" / "cennik_uslug_2026.pdf"
    if not pdf.is_file():
        pytest.skip("demo PDF not generated yet")
    out = text_mirror.render(pdf)
    assert out.files[".txt"].startswith("[strona 1]\nCennik") and out.pages == 1


def test_docx_adds_header_and_footer_while_the_index_text_stays_the_same(tmp_path):
    path = _docx(tmp_path / "umowa.docx", first_page=True)
    body = extract(path)[0].text
    assert "KAD/7" not in body and "stopki" not in body  # what v2 indexes is unchanged
    text = text_mirror.render(path).files[".txt"]
    assert text.startswith("[nagłówek]\nPrzykładowa Sp. z o.o. — dokument wewnętrzny KAD/7\n\n"
                           "[nagłówek]\nPierwsza strona: pismo przewodnie\n\n")
    assert body in text and text.endswith("[stopka]\nStrona stopki: wersja 3 z 2026-02-01\n")


def test_docx_without_header_or_footer_is_just_the_body(tmp_path):
    from docx import Document

    doc = Document()
    doc.add_paragraph("Sama treść.")
    doc.save(tmp_path / "a.docx")
    assert text_mirror.render(tmp_path / "a.docx").files[".txt"] == "Sama treść.\n"


def test_eml_html_body_is_added_only_when_it_has_words_the_plain_body_lacks(tmp_path):
    same = _eml(tmp_path / "a.eml", "Proszę o urlop 3 dni.", "<p>Proszę o <b>urlop</b> 3 dni.</p>")
    assert "[treść HTML wiadomości]" not in text_mirror.render(same).files[".txt"]
    more = _eml(tmp_path / "b.eml", "Proszę o urlop.", "<p>Proszę o urlop.</p><p>Zastępstwo: Jan z działu IT.</p>")
    text = text_mirror.render(more).files[".txt"]
    assert text.index("Proszę o urlop.") < text.index("[treść HTML wiadomości]\n") < text.index("Zastępstwo: Jan")
    only_html = _eml(tmp_path / "c.eml", "x", None)
    assert "[treść HTML" not in text_mirror.render(only_html).files[".txt"]


def test_xlsx_gets_sheet_markers_and_one_csv_per_sheet_aligned_to_a1(tmp_path):
    path = _xlsx(tmp_path / "rejestr.xlsx")
    out = text_mirror.render(path)
    assert list(out.files) == [".txt", ".Faktury.csv", ".A_B.csv", ".A_B_2.csv"]  # no CSV for the empty sheet
    assert out.sheets == ["Faktury", "A|B", "A_B"]
    assert out.files[".txt"].startswith("[arkusz Faktury]\n") and "[arkusz A|B]\nx |  | z" in out.files[".txt"]
    rows = list(csv.reader(io.StringIO(out.files[".Faktury.csv"])))
    assert rows[2] == ["", "Nr", "Data", "Kwota"] and rows[3] == ["", "F/1", "2026-03-02", "1500"]
    assert rows[4] == ["", "F/2", "2026-03-09", "320.5"] and len(rows) == 5 and {len(r) for r in rows} == {4}
    assert list(csv.reader(io.StringIO(out.files[".A_B.csv"]))) == [["x", "", "z"]]


def test_csv_document_is_mirrored_whole(tmp_path):
    path = tmp_path / "dane.csv"
    path.write_text("nr;kwota\n" + "".join(f"{i};{i * 10}\n" for i in range(CSV_ROWS + 5)), encoding="cp1250")
    assert "pokazano" in extract(path)[0].text  # the index reads the first rows only
    text = text_mirror.render(path).files[".txt"]
    assert text.startswith("nr;kwota\n0;0\n") and text.endswith(f"{CSV_ROWS + 4};{(CSV_ROWS + 4) * 10}\n")


def test_unreadable_document_gets_a_one_line_reason(tmp_path):
    out = text_mirror.render(tmp_path / "zepsuty.pdf", "zepsuty.pdf: no text found")
    assert out.files == {".txt": "[nie udało się odczytać tekstu dokumentu: zepsuty.pdf: no text found]\n"}
    assert out.error == "zepsuty.pdf: no text found"


def test_text_is_cleaned_for_grep():
    assert text_mirror._clean("a\r\nb\rc\x00d\n\n\n") == "a\nb\ncd\n"


def test_unsafe_source_paths_are_never_written(tmp_path):
    for bad in ("../x.txt", "/etc/x", "a/../../x", "a//b", "./a", "", "a\\b"):
        assert not text_mirror._safe_source(bad)
    assert text_mirror._safe_source("dodane/oferta 1.pdf")
    doc = tmp_path / "doc.txt"
    doc.write_text("tekst", encoding="utf-8")
    index = tmp_path / "index"
    assert text_mirror.write(index, "c", [text_mirror.Doc(doc, "../uciekinier.txt", "x")])["written"] == 0
    assert not (tmp_path / "uciekinier.txt.txt").exists() and not (index / "uciekinier.txt.txt").exists()


# --------------------------------------------------------------------------- in step with the index

class FakeCollection:
    def __init__(self):
        self.rows: dict[str, tuple[str, dict]] = {}

    def add_documents(self, docs, ids):
        for doc, cid in zip(docs, ids):
            self.rows[cid] = (doc.page_content, dict(doc.metadata))

    def get(self, include=None, where=None):
        rows = [(cid, text, meta) for cid, (text, meta) in self.rows.items()
                if not where or all(meta.get(k) == v for k, v in where.items())]
        return {"ids": [r[0] for r in rows], "documents": [r[1] for r in rows], "metadatas": [r[2] for r in rows]}

    def delete(self, where):
        for cid in self.get(where=where)["ids"]:
            del self.rows[cid]


class FakeClient:
    def __init__(self):
        self.collection = FakeCollection()
        self.rebuilt = 0

    def get_collection(self, name):
        return self.collection

    def delete_collection(self, name):
        self.rebuilt += 1
        self.collection.rows.clear()


@pytest.fixture
def kb(tmp_path, monkeypatch):
    data, docs = tmp_path / "var", tmp_path / "docs"
    (docs / "kadry").mkdir(parents=True)
    monkeypatch.setenv("HAPILOT_DATA_DIR", str(data))
    monkeypatch.setenv("HAPILOT_DOCS_DIR", str(docs))
    monkeypatch.delenv("HAPILOT_ADDED_DOCS_DIR", raising=False)
    reset_settings()
    client = FakeClient()
    monkeypatch.setattr(knowledge_base, "chroma_client", lambda path=None: client)
    monkeypatch.setattr(knowledge_base, "vector_store", lambda collection=None, path=None: client.collection)
    (docs / "kadry" / "regulamin.txt").write_text("Urlop zgłaszamy 14 dni wcześniej.\n" * 60, encoding="utf-8")
    _docx(docs / "umowa.docx")
    _xlsx(docs / "rejestr.xlsx")
    (docs / "zepsuty.pdf").write_bytes(b"not a pdf at all")
    s = get_settings()
    ingest_data.ingest(docs, s.collection, path=s.chroma_path, added_dir=s.added_docs_dir)
    client.rebuilt = 0
    yield {"docs": docs, "client": client, "s": s, "mirror": text_mirror.mirror_dir(s.chroma_path, s.collection)}
    reset_settings()


def _check(kb, chunks=True):
    return text_mirror.check(kb["s"].chroma_path, kb["s"].collection, chunks=chunks)


def _manifest(kb):
    return text_mirror.read_manifest(kb["s"].chroma_path, kb["s"].collection)["files"]


def test_ingest_writes_a_mirror_matching_the_index(kb):
    report = _check(kb)
    assert report["ok"], report
    assert (report["documents"], report["mirrored"], report["unreadable"]) == (4, 4, ["zepsuty.pdf"])
    assert report["chunks"]["checked"] == len(kb["client"].collection.rows) > 4 and not report["chunks"]["not_found"]
    mirror = kb["mirror"]
    assert sorted(p.relative_to(mirror).as_posix() for p in mirror.rglob("*") if p.is_file()) == [
        "kadry/regulamin.txt.txt", "rejestr.xlsx.A_B.csv", "rejestr.xlsx.A_B_2.csv", "rejestr.xlsx.Faktury.csv",
        "rejestr.xlsx.txt", "umowa.docx.txt", "zepsuty.pdf.txt"]
    assert (mirror / "zepsuty.pdf.txt").read_text(encoding="utf-8").startswith("[nie udało się odczytać")
    listed = ingest_data.read_files(kb["s"].collection, kb["s"].chroma_path)
    assert {s: e["sha256"] for s, e in _manifest(kb).items()} == {s: e["sha256"] for s, e in listed.items()}


def test_unchanged_corpus_leaves_the_mirror_alone(kb):
    before = {p: p.stat().st_mtime_ns for p in kb["mirror"].rglob("*") if p.is_file()}
    s = kb["s"]
    ingest_data.ingest(kb["docs"], s.collection, path=s.chroma_path, added_dir=s.added_docs_dir)
    assert kb["client"].rebuilt == 0
    assert {p: p.stat().st_mtime_ns for p in kb["mirror"].rglob("*") if p.is_file()} == before


def test_single_document_changes_keep_the_mirror_in_step(kb):
    s, docs, mirror = kb["s"], kb["docs"], kb["mirror"]
    (docs / "kadry" / "regulamin.txt").write_text("Nowa wersja: urlop zgłaszamy 7 dni wcześniej.", encoding="utf-8")
    (docs / "nowy.md").write_text("# Parking\nStrefa B.", encoding="utf-8")
    ingest_data.index_entries([(docs / "kadry" / "regulamin.txt", "kadry/regulamin.txt"), (docs / "nowy.md", "nowy.md")],
                              collection=s.collection, docs_dir=docs, added_dir=s.added_docs_dir, path=s.chroma_path)
    assert (mirror / "kadry" / "regulamin.txt.txt").read_text(encoding="utf-8").startswith("Nowa wersja")
    assert (mirror / "nowy.md.txt").read_text(encoding="utf-8") == "# Parking\nStrefa B.\n"
    assert _check(kb)["ok"]

    (docs / "kadry" / "regulamin.txt").unlink()
    ingest_data.remove_sources(["kadry/regulamin.txt"], collection=s.collection, docs_dir=docs,
                               added_dir=s.added_docs_dir, path=s.chroma_path)
    assert not (mirror / "kadry").exists()  # the emptied folder goes too
    assert "kadry/regulamin.txt" not in _manifest(kb) and _check(kb)["ok"]
    assert kb["client"].rebuilt == 0


def test_documents_added_from_the_chat_are_mirrored_under_dodane(kb, tmp_path):
    s = kb["s"]
    upload = tmp_path / "oferta.txt"
    upload.write_text("Oferta na serwis klimatyzacji.", encoding="utf-8")
    result = ingest_data.add_documents([upload], docs_dir=kb["docs"], added_dir=s.added_docs_dir,
                                       collection=s.collection, path=s.chroma_path)
    assert result["added"] == ["dodane/oferta.txt"]
    assert (kb["mirror"] / "dodane" / "oferta.txt.txt").read_text(encoding="utf-8") == "Oferta na serwis klimatyzacji.\n"
    assert _check(kb)["ok"]


def test_sync_rebuilds_a_deleted_mirror_without_touching_the_index(kb):
    import shutil

    s = kb["s"]
    rows = dict(kb["client"].collection.rows)
    shutil.rmtree(kb["mirror"])
    report = _check(kb, chunks=False)
    assert not report["ok"] and len(report["missing_files"]) == 7
    result = text_mirror.sync(s.chroma_path, s.collection, kb["docs"], s.added_docs_dir)
    assert result["written"] == 4 and result["pending"] == [] and result["originals_missing"] == []
    assert _check(kb)["ok"] and kb["client"].collection.rows == rows and kb["client"].rebuilt == 0


def test_sync_rewrites_stale_entries_and_removes_files_nobody_owns(kb):
    s = kb["s"]
    manifest = text_mirror.manifest_path(s.chroma_path, s.collection)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["files"]["umowa.docx"]["version"] = 0
    data["files"]["stary.txt"] = {"version": 1, "sha256": "x", "outputs": ["stary.txt.txt"]}
    manifest.write_text(json.dumps(data), encoding="utf-8")
    (kb["mirror"] / "stary.txt.txt").write_text("stary", encoding="utf-8")
    (kb["mirror"] / "zgubiony.csv").write_text("a,b", encoding="utf-8")
    report = _check(kb, chunks=False)
    assert (report["stale"], report["extra"], report["orphans"]) == (["umowa.docx"], ["stary.txt"], ["zgubiony.csv"])
    result = text_mirror.sync(s.chroma_path, s.collection, kb["docs"], s.added_docs_dir)
    assert (result["written"], result["removed"], result["orphans_removed"]) == (1, 1, 1)
    assert _check(kb)["ok"]


def test_sync_leaves_a_document_changed_since_indexing_to_the_re_indexing(kb):
    s, docs = kb["s"], kb["docs"]
    (kb["mirror"] / "umowa.docx.txt").unlink()
    _docx(docs / "umowa.docx", first_page=True)  # different content, not indexed yet
    result = text_mirror.sync(s.chroma_path, s.collection, docs, s.added_docs_dir)
    assert result["pending"] == ["umowa.docx"] and not (kb["mirror"] / "umowa.docx.txt").exists()


def test_chunk_check_finds_a_mirror_that_does_not_hold_the_indexed_text(kb):
    (kb["mirror"] / "kadry" / "regulamin.txt.txt").write_text("coś innego\n", encoding="utf-8")
    report = _check(kb)
    assert not report["ok"] and {c["source"] for c in report["chunks"]["not_found"]} == {"kadry/regulamin.txt"}


def test_check_without_a_document_list_is_not_ok(tmp_path):
    report = text_mirror.check(tmp_path, "nic")
    assert report["ok"] is False and "per-document list" in report["problem"]


def test_a_mirror_failure_never_fails_indexing(kb, monkeypatch, capsys):
    s, docs = kb["s"], kb["docs"]

    def broken(*args, **kwargs):
        raise OSError("dysk pełny")

    monkeypatch.setattr(text_mirror, "write", broken)
    (docs / "nowy.txt").write_text("Nowy dokument.", encoding="utf-8")
    facts = ingest_data.index_entries([(docs / "nowy.txt", "nowy.txt")], collection=s.collection, docs_dir=docs,
                                      added_dir=s.added_docs_dir, path=s.chroma_path)
    assert facts["nowy.txt"]["chunks"] == 1 and "dysk pełny" in capsys.readouterr().out
    assert _check(kb, chunks=False)["missing"] == ["nowy.txt"]


def test_the_panels_folder_re_check_repairs_the_mirror(kb, monkeypatch):
    import shutil

    monkeypatch.setattr(knowledge_admin, "_queue", type(knowledge_admin._queue)())
    monkeypatch.setattr(knowledge_admin, "_errors", {})
    shutil.rmtree(kb["mirror"])
    (kb["docs"] / "dopisany.txt").write_text("Plik skopiowany do folderu na serwerze.", encoding="utf-8")
    knowledge_admin.rescan()
    report = _check(kb)
    assert report["ok"] and report["documents"] == 5
    assert (kb["mirror"] / "dopisany.txt.txt").is_file() and kb["client"].rebuilt == 0


def test_extract_bytes_is_unchanged_for_docx_with_header(tmp_path):
    path = _docx(tmp_path / "u.docx")
    assert extract_bytes("u.docx", path.read_bytes())[0].text == "Treść umowy serwisowej.\nUsługa | Cena"
