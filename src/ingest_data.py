"""Index company documents into the local knowledge base.

Usage:
    python -m src.ingest_data [--docs DIR] [--collection NAME] [--force]

Reads .txt, .md and .pdf files from the documents directory (default: HAPILOT_DOCS_DIR), splits
them into overlapping chunks, embeds them on CPU and stores them in the embedded ChromaDB.
A fingerprint of the corpus is stored next to the database; an unchanged corpus is not
re-indexed unless --force is given. Runs offline: the embedding model must already be cached
(``scripts/hapilotctl.py setup`` downloads it once).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.privacy import apply_offline_env

SUPPORTED = (".txt", ".md", ".pdf")
CHUNK_SIZE = 800      # characters; E5-base accepts 512 tokens
CHUNK_OVERLAP = 200


def corpus_files(docs_dir: Path) -> list[Path]:
    return sorted(p for p in docs_dir.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED)


def corpus_fingerprint(files: list[Path], docs_dir: Path) -> str:
    h = hashlib.sha256()
    for p in files:
        h.update(str(p.relative_to(docs_dir)).encode("utf-8"))
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def load_documents(files: list[Path]) -> list:
    from langchain_community.document_loaders import PyPDFLoader, TextLoader

    documents = []
    for path in files:
        try:
            loader = PyPDFLoader(str(path)) if path.suffix.lower() == ".pdf" else TextLoader(str(path), encoding="utf-8")
            documents.extend(loader.load())
            print(f"📄 Wczytano: {path.name}")
        except Exception as exc:
            print(f"❌ Błąd przy pliku {path.name}: {exc}")
    return documents


def ingest(docs_dir: Path, collection: str, force: bool = False) -> int:
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    from src import knowledge_base
    from src.config import get_settings

    files = corpus_files(docs_dir)
    if not files:
        print(f"⚠️ Brak dokumentów ({', '.join(SUPPORTED)}) w {docs_dir}")
        return 0
    fingerprint = corpus_fingerprint(files, docs_dir)
    marker = get_settings().chroma_path / f"{collection}.fingerprint.json"
    if not force and marker.is_file() and json.loads(marker.read_text()).get("fingerprint") == fingerprint:
        print(f"✅ Korpus bez zmian ({len(files)} plików) — pomijam ponowne indeksowanie.")
        return 0

    documents = load_documents(files)
    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP,
                                              separators=["\n\n", "\n", ". ", " ", ""])
    chunks = splitter.split_documents(documents)
    counters: dict[str, int] = {}
    for chunk in chunks:
        source = chunk.metadata.get("source", "")
        index = counters.get(source, 0)
        counters[source] = index + 1
        chunk.metadata["chunk_id"] = knowledge_base.chunk_id(source, index, chunk.page_content)
    print(f"🔪 {len(documents)} dokument(y/ów) → {len(chunks)} fragmentów")

    client = knowledge_base.chroma_client()
    try:
        client.delete_collection(collection)
    except Exception:
        pass  # collection did not exist
    knowledge_base.reset_cache()
    store = knowledge_base.vector_store(collection=collection)
    for i in range(0, len(chunks), 100):
        batch = chunks[i:i + 100]
        store.add_documents(batch, ids=[c.metadata["chunk_id"] for c in batch])
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"fingerprint": fingerprint, "files": len(files), "chunks": len(chunks),
                                  "docs_dir": str(docs_dir)}, indent=2))
    print(f"🎉 Zaindeksowano {len(chunks)} fragmentów z {len(files)} plików do kolekcji {collection!r}.")
    return len(chunks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--docs", type=Path, help="documents directory (default: HAPILOT_DOCS_DIR)")
    parser.add_argument("--collection", help="collection name (default: HAPILOT_COLLECTION)")
    parser.add_argument("--force", action="store_true", help="re-index even if the corpus is unchanged")
    args = parser.parse_args(argv)

    apply_offline_env()
    from src.config import get_settings

    settings = get_settings()
    ingest((args.docs or settings.docs_dir).resolve(), args.collection or settings.collection, force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
