"""Attachments: copied into the workspace, inlined when short, indexed when long (same code for UI and evaluation)."""

from src import attachments, run_context


def test_short_attachments_are_inlined(tmp_path, session):
    f = tmp_path / "notatka.txt"
    f.write_text("Spotkanie w piątek o 10:00.", encoding="utf-8")
    bad = tmp_path / "skan.pdf"
    bad.write_bytes(b"%PDF-broken")
    prepared = attachments.prepare([f, bad], index_path=tmp_path / "idx")
    assert prepared.collection is None
    assert "[Załącznik: notatka.txt]" in prepared.message_block and "Spotkanie w piątek" in prepared.message_block
    assert "Nie udało się odczytać tekstu z: skan.pdf" in prepared.message_block
    assert (run_context.workspace_dir() / "notatka.txt").is_file() and (run_context.workspace_dir() / "skan.pdf").is_file()


def test_long_attachments_are_indexed_not_inlined(tmp_path, session, monkeypatch):
    indexed = {}
    monkeypatch.setattr(attachments, "_index", lambda texts, path, name: indexed.update(texts=texts, path=path, name=name))
    f = tmp_path / "raport.txt"
    f.write_text("treść " * 5000, encoding="utf-8")
    prepared = attachments.prepare([f], index_path=tmp_path / "idx", inline_limit=1000)
    assert prepared.collection == (str(tmp_path / "idx"), attachments.collection_name(run_context.session_id()))
    assert "[Załącznik:" not in prepared.message_block and "przeszukuję" in prepared.message_block
    assert indexed["texts"][0][0] == "raport.txt"


def test_name_collisions_and_unsafe_names(tmp_path, session):
    a = tmp_path / "a"
    a.mkdir()
    (a / "dane.txt").write_text("1")
    b = tmp_path / "b"
    b.mkdir()
    (b / "dane.txt").write_text("2")
    prepared = attachments.prepare([a / "dane.txt", b / "dane.txt"], index_path=tmp_path / "idx",
                                   display_names=["dane.txt", "../../etc/passwd"])
    assert prepared.names == ["dane.txt", "passwd"]
    prepared = attachments.prepare([a / "dane.txt"], index_path=tmp_path / "idx")
    assert prepared.names == ["dane_1.txt"]


def test_collection_names_are_chroma_compatible():
    name = attachments.collection_name("Jakaś sesja / 123")
    assert name.startswith("att_") and len(name) <= 63 and name.replace("_", "").isalnum()


def test_long_attachment_is_chunked_into_its_own_collection(tmp_path, session, monkeypatch):
    from src import knowledge_base

    added, deleted = [], []

    class Store:
        def add_documents(self, docs, ids):
            assert len(docs) == len(ids) <= 64
            added.extend(docs)

    class Client:
        def delete_collection(self, name):
            deleted.append(name)

    monkeypatch.setattr(knowledge_base, "chroma_client", lambda path=None: Client())
    monkeypatch.setattr(knowledge_base, "vector_store", lambda collection=None, path=None: Store())
    f = tmp_path / "raport.txt"
    f.write_text("\n\n".join(f"Akapit {i}: treść raportu kwartalnego." for i in range(2000)), encoding="utf-8")
    prepared = attachments.prepare([f], index_path=tmp_path / "idx", inline_limit=1000)
    assert deleted == [prepared.collection[1]]  # a re-upload replaces the session's attachment collection
    assert len(added) > 64 and {d.metadata["source"] for d in added} == {"załącznik:raport.txt"}
    assert len({d.metadata["chunk_id"] for d in added}) == len(added)
