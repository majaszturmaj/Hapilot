from types import SimpleNamespace

from src import knowledge_base
from src.ingest_data import corpus_files, corpus_fingerprint


class _Store:
    def __init__(self):
        self.queries = []

    def similarity_search_with_relevance_scores(self, query, k):
        self.queries.append((query, k))
        doc = SimpleNamespace(page_content="Wniosek składa się 14 dni wcześniej.",
                              metadata={"source": "/x/regulamin_urlopow.txt", "chunk_id": "regulamin_urlopow.txt#0:abc"})
        return [(doc, 0.87654)]


def test_search_returns_structured_chunks_with_e5_prefix():
    store = _Store()
    chunks = knowledge_base.search("termin wniosku urlopowego", k=3, store=store)
    assert store.queries == [("query: termin wniosku urlopowego", 3)]
    assert chunks[0].file == "regulamin_urlopow.txt" and chunks[0].score == 0.8765
    assert knowledge_base.format_chunks(chunks).startswith("--- ŹRÓDŁO: regulamin_urlopow.txt ---")
    assert knowledge_base.format_chunks([]) == "Brak wyników."


def test_chunk_ids_are_stable_and_distinct():
    a = knowledge_base.chunk_id("/docs/a.txt", 0, "tekst")
    assert a == knowledge_base.chunk_id("/other/a.txt", 0, "tekst") and a.startswith("a.txt#0:")
    assert a != knowledge_base.chunk_id("/docs/a.txt", 1, "tekst")


def test_corpus_fingerprint_changes_with_content(tmp_path):
    (tmp_path / "a.txt").write_text("jeden")
    (tmp_path / "skip.docx").write_bytes(b"x")
    files = corpus_files(tmp_path)
    assert [f.name for f in files] == ["a.txt"]
    first = corpus_fingerprint(files, tmp_path)
    (tmp_path / "a.txt").write_text("dwa")
    assert corpus_fingerprint(files, tmp_path) != first


def test_demo_corpus_is_present_and_fictional():
    from src.config import PROJECT_ROOT

    files = corpus_files(PROJECT_ROOT / "examples" / "demo_company" / "docs")
    assert len(files) >= 3
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert "Przykładowa" in text.replace("PRZYKŁADOWA", "Przykładowa")
        for addr in __import__("re").findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text):
            assert addr.endswith("@example.com"), addr
