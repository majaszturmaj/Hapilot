"""Hybrid retrieval (BM25 + dense, RRF), chunk ids, corpus fingerprint, demo corpus."""

import re
from pathlib import Path

import pytest

from src import knowledge_base, run_context
from src.ingest_data import corpus_files, corpus_fingerprint

CORPUS = {
    "c1": ("kadry/regulamin_urlopow.txt", "Wniosek urlopowy składa się co najmniej 14 dni kalendarzowych przed urlopem."),
    "c2": ("jakosc/reklamacje.txt", "Odpowiedź na reklamację wysyłamy w ciągu 14 dni. Odpowiada Dział Jakości."),
    "c3": ("it/bezpieczenstwo.txt", "Hasło ma co najmniej 12 znaków. Incydenty zgłaszamy w ciągu 2 godzin."),
    "c4": ("finanse/rejestr.xlsx", "R-7 | 2026-03-02 | 1500 | uznana"),
}


class FakeBackend:
    """Dense ranking: a fixed order per query; all chunks for BM25."""

    def __init__(self, dense_order):
        self.dense_order = dense_order
        self.queries = []

    def dense(self, path, collection, query, k):
        self.queries.append((collection, query))
        ids = self.dense_order.get(collection, [])
        return [(cid, CORPUS[cid][1], {"source": CORPUS[cid][0], "chunk_id": cid}, 0.9 - i * 0.1) for i, cid in enumerate(ids[:k])]

    def all_chunks(self, path, collection):
        ids = self.dense_order.get(collection, [])
        return [(cid, CORPUS[cid][1], {"source": CORPUS[cid][0], "chunk_id": cid}) for cid in ids]


@pytest.fixture
def backend():
    fake = FakeBackend({"corpus": ["c3", "c2", "c1"], "attachments": ["c4"]})
    knowledge_base.set_backend(fake)
    yield fake
    knowledge_base.set_backend(None)


def test_tokenizer_stems_and_drops_stop_words():
    assert knowledge_base.tokenize("Wnioski urlopowe są w regulaminie, 14 dni") == ["wniosk", "urlopo", "regula", "14", "dni"]


def test_hybrid_ranks_lexical_match_first(backend):
    # dense prefers c3, but only c1 shares the words → RRF puts c1 on top
    chunks = knowledge_base.search("wniosek urlopowy 14 dni", k=2, targets=[(Path("/x"), "corpus")])
    assert [c.chunk_id for c in chunks][0] == "c1"
    top = chunks[0]
    assert top.file == "kadry/regulamin_urlopow.txt" and top.score_bm25 > 0 and top.score_dense is not None and top.rank == 1
    assert knowledge_base.format_chunks(chunks).startswith("--- ŹRÓDŁO: kadry/regulamin_urlopow.txt ---")


def test_attachment_collection_is_searched_with_the_corpus(backend):
    with run_context.use_context(kb_collection="corpus", extra_collections=[("/y", "attachments")]):
        chunks = knowledge_base.search("R-7 kwota uznana", k=3)
    assert {c.collection for c in chunks} == {"corpus", "attachments"}
    assert chunks[0].chunk_id == "c4"


def test_missing_collection_is_skipped_not_fatal(backend):
    class Broken(FakeBackend):
        def dense(self, *a):
            raise ValueError("Collection does not exist")

    knowledge_base.set_backend(Broken({}))
    assert knowledge_base.search("cokolwiek", targets=[(Path("/x"), "none")]) == []
    assert knowledge_base.format_chunks([]) == "Brak wyników."


def test_chunk_ids_are_stable_and_distinct():
    a = knowledge_base.chunk_id("kadry/a.txt", 0, "tekst")
    assert a == knowledge_base.chunk_id("kadry/a.txt", 0, "tekst") and a.startswith("kadry/a.txt#0:")
    assert a != knowledge_base.chunk_id("kadry/a.txt", 1, "tekst") != knowledge_base.chunk_id("jakosc/a.txt", 0, "tekst")


def test_corpus_fingerprint_changes_with_content(tmp_path):
    (tmp_path / "a.txt").write_text("jeden")
    (tmp_path / "b.doc").write_bytes(b"x")  # unsupported (old Word format)
    files = corpus_files(tmp_path)
    assert [f.name for f in files] == ["a.txt"]
    first = corpus_fingerprint(files, tmp_path)
    (tmp_path / "a.txt").write_text("dwa")
    assert corpus_fingerprint(files, tmp_path) != first


def test_demo_corpus_is_present_and_fictional():
    from src.config import PROJECT_ROOT
    from src.extractors import as_text, extract

    files = corpus_files(PROJECT_ROOT / "examples" / "demo_company" / "docs")
    assert len(files) >= 3
    for f in files:
        text = as_text(extract(f))
        for addr in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text):
            assert addr.endswith("@example.com"), (f.name, addr)


def test_collection_rebuilt_by_another_process_is_reloaded(monkeypatch):
    # regression: after "hapilotctl ingest --force" the running UI kept a handle to the deleted collection
    # and found nothing until restarted
    resets = []
    monkeypatch.setattr(knowledge_base, "reset_cache", lambda: resets.append(1))

    class Rebuilt(FakeBackend):
        calls = 0

        def dense(self, *a):
            Rebuilt.calls += 1
            if Rebuilt.calls == 1:
                raise ValueError("Collection [2cdc…] does not exist.")
            return super().dense(*a)

    knowledge_base.set_backend(Rebuilt({"corpus": ["c1"]}))
    resets.clear()  # set_backend resets the cache too
    try:
        chunks = knowledge_base.search("wniosek urlopowy", targets=[(Path("/x"), "corpus")])
        assert [c.chunk_id for c in chunks] == ["c1"] and resets == [1]
    finally:
        knowledge_base.set_backend(None)
