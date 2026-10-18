"""Index company documents into the local knowledge base.

Usage:
    python -m src.ingest_data [--docs DIR] [--collection NAME] [--force]

Reads .pdf, .docx, .eml, .xlsx, .txt and .md files (recursively) from the documents directory
(default: HAPILOT_DOCS_DIR), extracts their text (src/extractors.py), splits it into overlapping
chunks, embeds them on CPU and stores them in the embedded ChromaDB. Chunks keep the file path
relative to the documents directory, and the page (PDF) or sheet (XLSX). Files that cannot be read
are reported, never silently dropped. A fingerprint of the corpus (plus the index version and the
embedding model) is stored next to the database; an unchanged corpus is not re-indexed unless
--force is given. Runs offline: the embedding model must already be cached (``hapilotctl.py setup``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.extractors import SUPPORTED, ExtractionError, extract
from src.privacy import apply_offline_env

CHUNK_SIZE = 800      # characters; E5-base accepts 512 tokens
CHUNK_OVERLAP = 200


def corpus_files(docs_dir: Path) -> list[Path]:
    return sorted(p for p in docs_dir.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED
                  and not any(part.startswith(".") for part in p.relative_to(docs_dir).parts))


def corpus_fingerprint(files: list[Path], docs_dir: Path) -> str:
    h = hashlib.sha256()
    for p in files:
        h.update(p.relative_to(docs_dir).as_posix().encode("utf-8"))
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def split_documents(files: list[Path], docs_dir: Path) -> tuple[list, list[dict]]:
    """LangChain documents (one per chunk) with metadata, and the files that could not be read."""
    from langchain_core.documents import Document
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    from src import knowledge_base

    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP,
                                              separators=["\n\n", "\n", ". ", " ", ""])
    chunks, skipped = [], []
    for path in files:
        rel = path.relative_to(docs_dir).as_posix()
        try:
            segments = extract(path)
        except ExtractionError as exc:
            skipped.append({"file": rel, "reason": str(exc)})
            print(f"❌ Pominięto {rel}: {exc}")
            continue
        index = 0
        for seg in segments:
            meta = {"source": rel}
            if "page" in seg.meta:
                meta["page"] = seg.meta["page"]
            if "sheet" in seg.meta:
                meta["sheet"] = seg.meta["sheet"]
            for piece in splitter.split_text(seg.text):
                # spreadsheet rows mean little without their column names
                text = f"[kolumny: {seg.meta['header']}]\n{piece}" if seg.meta.get("header") and not piece.startswith(seg.meta["header"]) else piece
                chunks.append(Document(page_content=text, metadata={**meta, "chunk_id": knowledge_base.chunk_id(rel, index, text)}))
                index += 1
        print(f"📄 Wczytano: {rel} ({index} fragm.)")
    return chunks, skipped


def ingest(docs_dir: Path, collection: str, path: Optional[Path] = None, force: bool = False) -> dict:
    """Index ``docs_dir`` into ``collection`` (at ``path``, default: the UI's knowledge base). Returns statistics."""
    from src import knowledge_base
    from src.config import get_settings

    settings = get_settings()
    path = Path(path or settings.chroma_path)
    files = corpus_files(docs_dir)
    fingerprint = corpus_fingerprint(files, docs_dir)
    signature = {"fingerprint": fingerprint, "index_version": knowledge_base.INDEX_VERSION,
                 "embedding_model": settings.embedding_model, "chunk_size": CHUNK_SIZE, "chunk_overlap": CHUNK_OVERLAP}
    marker = path / f"{collection}.fingerprint.json"
    if not force and marker.is_file():
        saved = json.loads(marker.read_text())
        if all(saved.get(k) == v for k, v in signature.items()):
            print(f"✅ Korpus bez zmian ({len(files)} plików) — pomijam ponowne indeksowanie.")
            return saved
    if not files:
        print(f"⚠️ Brak dokumentów ({', '.join(SUPPORTED)}) w {docs_dir}")

    chunks, skipped = split_documents(files, docs_dir)
    client = knowledge_base.chroma_client(path)
    try:
        client.delete_collection(collection)
    except Exception:
        pass  # collection did not exist
    knowledge_base.reset_cache()
    store = knowledge_base.vector_store(collection=collection, path=path)
    for i in range(0, len(chunks), 64):
        batch = chunks[i:i + 64]
        store.add_documents(batch, ids=[c.metadata["chunk_id"] for c in batch])
    stats = {**signature, "files": len(files), "chunks": len(chunks), "skipped": skipped, "collection": collection,
             "docs_dir": str(docs_dir)}
    path.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(stats, indent=2, ensure_ascii=False))
    print(f"🎉 Zaindeksowano {len(chunks)} fragmentów z {len(files) - len(skipped)} plików do kolekcji {collection!r}"
          + (f"; pominięto {len(skipped)}" if skipped else "") + ".")
    return stats


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
