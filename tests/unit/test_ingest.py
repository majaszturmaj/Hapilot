"""Indexing: chunk metadata, unreadable files reported, unchanged corpus not re-indexed (fake vector store)."""

import json
import shutil
from pathlib import Path

import pytest

from src import ingest_data, knowledge_base
from src.config import PROJECT_ROOT

DEMO = PROJECT_ROOT / "examples" / "demo_company" / "docs"


class FakeStore:
    def __init__(self):
        self.added = []

    def add_documents(self, docs, ids):
        assert len(docs) == len(ids) <= 64
        self.added.extend(zip(ids, docs))


class FakeClient:
    def __init__(self):
        self.deleted = []

    def delete_collection(self, name):
        self.deleted.append(name)
        raise ValueError("does not exist")  # first indexing: nothing to delete, must not be fatal


@pytest.fixture
def fake_chroma(monkeypatch):
    store, client = FakeStore(), FakeClient()
    monkeypatch.setattr(knowledge_base, "chroma_client", lambda path=None: client)
    monkeypatch.setattr(knowledge_base, "vector_store", lambda collection=None, path=None: store)
    return store, client


def test_demo_corpus_chunks_keep_source_page_and_sheet():
    files = ingest_data.corpus_files(DEMO)
    chunks, skipped = ingest_data.split_documents(files, DEMO)
    assert skipped == []
    sources = {c.metadata["source"] for c in chunks}
    assert sources == {f.relative_to(DEMO).as_posix() for f in files}
    pdf = [c for c in chunks if c.metadata["source"].endswith(".pdf")]
    xlsx = [c for c in chunks if c.metadata["source"].endswith(".xlsx")]
    assert pdf and all(c.metadata["page"] >= 1 for c in pdf)
    assert xlsx and all(c.metadata.get("sheet") for c in xlsx)
    ids = [c.metadata["chunk_id"] for c in chunks]
    assert len(ids) == len(set(ids))
    assert max(len(c.page_content) for c in chunks if not c.metadata["source"].endswith(".xlsx")) <= ingest_data.CHUNK_SIZE


def test_long_sheet_chunks_repeat_the_column_names(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Rejestr"
    ws.append(["Nr", "Klient", "Kwota (zł)"])
    for i in range(200):
        ws.append([f"R-{i:03d}", f"Klient testowy {i}", 100 + i])
    wb.save(tmp_path / "rejestr.xlsx")
    chunks, _ = ingest_data.split_documents([tmp_path / "rejestr.xlsx"], tmp_path)
    assert len(chunks) > 3 and all(c.metadata["sheet"] == "Rejestr" for c in chunks)
    assert chunks[0].page_content.startswith("Nr | Klient | Kwota (zł)")
    assert all(c.page_content.startswith("[kolumny: Nr | Klient | Kwota (zł)]\n") for c in chunks[1:])


def test_ingest_reports_unreadable_files_and_skips_unchanged_corpus(tmp_path, fake_chroma):
    store, client = fake_chroma
    docs = tmp_path / "docs"
    (docs / "kadry").mkdir(parents=True)
    shutil.copy(DEMO / "regulamin_urlopow.txt", docs / "kadry" / "regulamin_urlopow.txt")
    (docs / "zepsuty.pdf").write_bytes(b"not a pdf at all")
    (docs / ".ukryty.txt").write_text("pominięty")
    db = tmp_path / "chroma"

    stats = ingest_data.ingest(docs, "test", path=db)
    assert stats["files"] == 2 and stats["chunks"] == len(store.added) > 0
    assert [s["file"] for s in stats["skipped"]] == ["zepsuty.pdf"]
    assert client.deleted == ["test"]
    assert {doc.metadata["source"] for _, doc in store.added} == {"kadry/regulamin_urlopow.txt"}
    marker = json.loads((db / "test.fingerprint.json").read_text())
    assert marker["fingerprint"] == stats["fingerprint"] and marker["index_version"] == knowledge_base.INDEX_VERSION

    added = len(store.added)
    assert ingest_data.ingest(docs, "test", path=db)["fingerprint"] == stats["fingerprint"]
    assert len(store.added) == added and client.deleted == ["test"]  # unchanged: nothing re-indexed

    ingest_data.ingest(docs, "test", path=db, force=True)
    assert len(store.added) == 2 * added and client.deleted == ["test", "test"]


def test_changed_corpus_is_reindexed(tmp_path, fake_chroma):
    store, _ = fake_chroma
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("Pierwsza wersja dokumentu.", encoding="utf-8")
    first = ingest_data.ingest(docs, "c", path=tmp_path / "db")
    (docs / "a.txt").write_text("Druga wersja dokumentu.", encoding="utf-8")
    second = ingest_data.ingest(docs, "c", path=tmp_path / "db")
    assert first["fingerprint"] != second["fingerprint"]
    assert [d.page_content for _, d in store.added] == ["Pierwsza wersja dokumentu.", "Druga wersja dokumentu."]


def test_cli_uses_settings_and_offline_mode(monkeypatch, tmp_path):
    from src.config import get_settings

    calls = {}
    monkeypatch.setattr(ingest_data, "ingest", lambda docs, collection, force=False, added_dir=None: calls.update(
        docs=docs, collection=collection, force=force, added_dir=added_dir))
    monkeypatch.setattr(ingest_data, "apply_offline_env", lambda: calls.setdefault("offline", True))
    assert ingest_data.main(["--docs", str(tmp_path), "--collection", "x", "--force"]) == 0
    assert calls == {"offline": True, "docs": Path(tmp_path).resolve(), "collection": "x", "force": True,
                     "added_dir": get_settings().added_docs_dir}


def test_added_documents_are_indexed_at_once_and_kept_by_the_next_ingest(tmp_path, fake_chroma):
    store, client = fake_chroma
    docs, added, db = tmp_path / "docs", tmp_path / "added", tmp_path / "db"
    docs.mkdir()
    (docs / "regulamin.txt").write_text("Urlop wypoczynkowy zgłaszamy 14 dni wcześniej.", encoding="utf-8")
    first = ingest_data.ingest(docs, "kb", path=db, added_dir=added)
    assert first["files"] == 1

    upload = tmp_path / "upload"
    upload.mkdir()
    (upload / "regulamin.txt").write_text("Nowa zasada: parking dla gości w strefie B.", encoding="utf-8")
    (upload / "skan.pdf").write_bytes(b"not a pdf")
    result = ingest_data.add_documents([upload / "regulamin.txt", upload / "skan.pdf"], docs_dir=docs, added_dir=added,
                                       collection="kb", path=db)
    assert result["added"] == ["dodane/regulamin.txt"] and result["chunks"] == 1
    assert [s["file"] for s in result["skipped"]] == ["dodane/skan.pdf"]
    assert sorted(p.name for p in added.iterdir()) == ["regulamin.txt"]  # the unreadable file is not kept
    assert store.added[-1][1].metadata["source"] == "dodane/regulamin.txt"
    assert client.deleted == ["kb"]  # the collection was not rebuilt

    added_count = len(store.added)
    again = ingest_data.ingest(docs, "kb", path=db, added_dir=added)
    assert again["files"] == 2 and len(store.added) == added_count  # the fingerprint already covers the added file

    again = ingest_data.add_documents([upload / "regulamin.txt"], docs_dir=docs, added_dir=added, collection="kb", path=db)
    assert again["added"] == [] and again["already"] == ["dodane/regulamin.txt"]  # same content: not added twice
    assert len(store.added) == added_count

    (upload / "regulamin.txt").write_text("Zmiana: parking dla gości w strefie C.", encoding="utf-8")
    second = ingest_data.add_documents([upload / "regulamin.txt"], docs_dir=docs, added_dir=added, collection="kb", path=db)
    assert second["added"] == ["dodane/regulamin_1.txt"]  # same name, other content → numbered, nothing overwritten
