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

Documents that users add from the chat ("dodaj ten plik do bazy wiedzy") are kept in
HAPILOT_ADDED_DOCS_DIR and indexed with the company documents under the path prefix ``dodane/``;
``add_documents`` indexes such a file at once, without rebuilding the index.
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
ADDED_PREFIX = "dodane"  # path prefix of documents added from the chat


def corpus_files(docs_dir: Path) -> list[Path]:
    return sorted(p for p in docs_dir.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED
                  and not any(part.startswith(".") for part in p.relative_to(docs_dir).parts))


def corpus_fingerprint(files: list[Path], docs_dir: Path) -> str:
    return _fingerprint([(p, p.relative_to(docs_dir).as_posix()) for p in files])


def _fingerprint(entries: list[tuple[Path, str]]) -> str:
    h = hashlib.sha256()
    for path, rel in entries:
        h.update(rel.encode("utf-8"))
        h.update(hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest()


def corpus_entries(docs_dir: Path, added_dir: Optional[Path] = None) -> list[tuple[Path, str]]:
    """(file, path shown as source) for the company documents and, if given, the documents added from the chat."""
    entries = [(p, p.relative_to(docs_dir).as_posix()) for p in corpus_files(docs_dir)]
    if added_dir is not None and added_dir.is_dir():
        entries += [(p, f"{ADDED_PREFIX}/{p.relative_to(added_dir).as_posix()}") for p in corpus_files(added_dir)]
    return entries


def split_documents(files: list[Path], docs_dir: Path) -> tuple[list, list[dict]]:
    """LangChain documents (one per chunk) with metadata, and the files that could not be read."""
    return split_entries([(p, p.relative_to(docs_dir).as_posix()) for p in files])


def split_entries(entries: list[tuple[Path, str]]) -> tuple[list, list[dict]]:
    """As split_documents, for (file, source path) pairs."""
    from langchain_core.documents import Document
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    from src import knowledge_base

    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP,
                                              separators=["\n\n", "\n", ". ", " ", ""])
    chunks, skipped = [], []
    for path, rel in entries:
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


def _signature(fingerprint: str) -> dict:
    from src import knowledge_base
    from src.config import get_settings

    return {"fingerprint": fingerprint, "index_version": knowledge_base.INDEX_VERSION,
            "embedding_model": get_settings().embedding_model, "chunk_size": CHUNK_SIZE, "chunk_overlap": CHUNK_OVERLAP}


def _add_chunks(store, chunks: list) -> None:
    for i in range(0, len(chunks), 64):
        batch = chunks[i:i + 64]
        store.add_documents(batch, ids=[c.metadata["chunk_id"] for c in batch])


def ingest(docs_dir: Path, collection: str, path: Optional[Path] = None, force: bool = False,
           added_dir: Optional[Path] = None) -> dict:
    """Index ``docs_dir`` (and ``added_dir``, if given) into ``collection`` at ``path`` (default: the UI's knowledge base)."""
    from src import knowledge_base
    from src.config import get_settings

    path = Path(path or get_settings().chroma_path)
    entries = corpus_entries(docs_dir, added_dir)
    signature = _signature(_fingerprint(entries))
    marker = path / f"{collection}.fingerprint.json"
    if not force and marker.is_file():
        saved = json.loads(marker.read_text())
        if all(saved.get(k) == v for k, v in signature.items()):
            print(f"✅ Korpus bez zmian ({len(entries)} plików) — pomijam ponowne indeksowanie.")
            return saved
    if not entries:
        print(f"⚠️ Brak dokumentów ({', '.join(SUPPORTED)}) w {docs_dir}")

    chunks, skipped = split_entries(entries)
    client = knowledge_base.chroma_client(path)
    try:
        client.delete_collection(collection)
    except Exception:
        pass  # collection did not exist
    knowledge_base.reset_cache()
    _add_chunks(knowledge_base.vector_store(collection=collection, path=path), chunks)
    stats = {**signature, "files": len(entries), "chunks": len(chunks), "skipped": skipped, "collection": collection,
             "docs_dir": str(docs_dir)}
    path.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(stats, indent=2, ensure_ascii=False))
    print(f"🎉 Zaindeksowano {len(chunks)} fragmentów z {len(entries) - len(skipped)} plików do kolekcji {collection!r}"
          + (f"; pominięto {len(skipped)}" if skipped else "") + ".")
    return stats


def add_documents(files: list[Path], *, docs_dir: Path, added_dir: Path, collection: str,
                  path: Optional[Path] = None) -> dict:
    """Copy files into ``added_dir`` and index only them into an existing ``collection``.

    The corpus fingerprint is updated, so the next ``ingest`` of the same folders does not rebuild the index.
    A file whose content was added before is not added again; a different file with the same name as an
    earlier one gets a numbered name. Returns
    {"added": [source paths], "chunks": n, "skipped": [...], "already": [source paths of identical files added before]}.
    """
    import shutil

    from src import knowledge_base
    from src.config import get_settings

    path = Path(path or get_settings().chroma_path)
    added_dir.mkdir(parents=True, exist_ok=True)
    present = {hashlib.sha256(p.read_bytes()).hexdigest(): f"{ADDED_PREFIX}/{p.relative_to(added_dir).as_posix()}"
               for p in corpus_files(added_dir)}
    entries, already = [], []
    for src in files:
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        if digest in present:  # the same content was added before (e.g. a confirmation clicked twice)
            already.append(present[digest])
            continue
        target = added_dir / src.name
        n = 1
        while target.exists():
            target = added_dir / f"{src.stem}_{n}{src.suffix}"
            n += 1
        shutil.copyfile(src, target)
        entries.append((target, f"{ADDED_PREFIX}/{target.name}"))
        present[digest] = entries[-1][1]
    chunks, skipped = split_entries(entries)
    for item in skipped:  # an unreadable file is not kept
        (added_dir / Path(item["file"]).name).unlink(missing_ok=True)
    _add_chunks(knowledge_base.vector_store(collection=collection, path=path), chunks)
    knowledge_base.reset_cache()

    marker = path / f"{collection}.fingerprint.json"
    stats = json.loads(marker.read_text()) if marker.is_file() else {"collection": collection, "docs_dir": str(docs_dir)}
    everything = corpus_entries(docs_dir, added_dir)
    stats.update(_signature(_fingerprint(everything)), files=len(everything), chunks=stats.get("chunks", 0) + len(chunks))
    path.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(stats, indent=2, ensure_ascii=False))
    skipped_names = {Path(i["file"]).name for i in skipped}
    return {"added": [rel for p, rel in entries if p.name not in skipped_names], "chunks": len(chunks), "skipped": skipped,
            "already": already}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--docs", type=Path, help="documents directory (default: HAPILOT_DOCS_DIR)")
    parser.add_argument("--collection", help="collection name (default: HAPILOT_COLLECTION)")
    parser.add_argument("--force", action="store_true", help="re-index even if the corpus is unchanged")
    args = parser.parse_args(argv)

    apply_offline_env()
    from src.config import get_settings

    settings = get_settings()
    ingest((args.docs or settings.docs_dir).resolve(), args.collection or settings.collection, force=args.force,
           added_dir=settings.added_docs_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
