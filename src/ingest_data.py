"""Index company documents into the local knowledge base.

Usage:
    python -m src.ingest_data [--docs DIR] [--collection NAME] [--force]

Reads .pdf, .docx, .eml, .xlsx, .txt and .md files (recursively) from the documents directory
(default: HAPILOT_DOCS_DIR), extracts their text (src/extractors.py), splits it into overlapping
chunks, embeds them on CPU and stores them in the embedded ChromaDB. Chunks keep the file path
relative to the documents directory, and the page (PDF) or sheet (XLSX); chunks of scanned pages
read by OCR (src/ocr.py) are marked ``ocr``. Files that cannot be read are reported, never silently dropped. A fingerprint of the corpus (plus the index version and the
embedding model, and what the OCR can do) is stored next to the database; an unchanged corpus is not
re-indexed unless --force is given. Runs offline: the embedding model must already be cached (``hapilotctl.py setup``).

Documents that users add from the chat ("dodaj ten plik do bazy wiedzy") are kept in
HAPILOT_ADDED_DOCS_DIR and indexed with the company documents under the path prefix ``dodane/``;
``add_documents`` indexes such a file at once, without rebuilding the index.

Next to the fingerprint, ``<collection>.files.json`` keeps what the index holds per document (content hash, size,
chunks, pages, pages read by OCR, or why it could not be read). ``index_entries`` and ``remove_sources`` change
single documents (the agent panel's "📚 Baza wiedzy" and its folder re-check, src/knowledge_admin.py) and keep both
files current, so the next ``ingest`` of an unchanged folder does not rebuild the index. A per-document list that
does not belong to the current fingerprint (an index rebuilt by an older Hapilot) is rebuilt from the index.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
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


def split_entries(entries: list[tuple[Path, str]], facts: Optional[dict] = None) -> tuple[list, list[dict]]:
    """As split_documents, for (file, source path) pairs. ``facts``, if given, receives per source path what
    went into the index: chunks, pages / sheets, pages read by OCR and their mean confidence, or the error."""
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
            if facts is not None:
                facts[rel] = {"chunks": 0, "pages": page_count(path), "sheets": None, "ocr_pages": 0,
                              "ocr_confidence": None, "error": str(exc)}
            print(f"❌ Pominięto {rel}: {exc}")
            continue
        index = 0
        ocr_pages = [seg.meta for seg in segments if seg.meta.get("ocr")]
        for seg in segments:
            meta = {"source": rel}
            if "page" in seg.meta:
                meta["page"] = seg.meta["page"]
            if "sheet" in seg.meta:
                meta["sheet"] = seg.meta["sheet"]
            if seg.meta.get("ocr"):
                meta["ocr"] = True
            for piece in splitter.split_text(seg.text):
                # spreadsheet rows mean little without their column names
                text = f"[kolumny: {seg.meta['header']}]\n{piece}" if seg.meta.get("header") and not piece.startswith(seg.meta["header"]) else piece
                chunks.append(Document(page_content=text, metadata={**meta, "chunk_id": knowledge_base.chunk_id(rel, index, text)}))
                index += 1
        note, mean = "", None
        if ocr_pages:
            mean = round(sum(m["ocr_confidence"] for m in ocr_pages) / len(ocr_pages), 1)
            note = f"; OCR: {len(ocr_pages)} str., pewność {mean:.0f}%"
        if facts is not None:
            sheets = [s for s in segments if "sheet" in s.meta]
            facts[rel] = {"chunks": index, "pages": page_count(path), "sheets": len(sheets) or None,
                          "ocr_pages": len(ocr_pages), "ocr_confidence": mean, "error": None}
        print(f"📄 Wczytano: {rel} ({index} fragm.{note})")
    return chunks, skipped


def page_count(path: Path) -> Optional[int]:
    """Number of pages of a PDF (None for other formats or an unreadable file)."""
    if path.suffix.lower() != ".pdf":
        return None
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception:
        return None


def _signature(fingerprint: str) -> dict:
    from src import knowledge_base, ocr
    from src.config import get_settings

    return {"fingerprint": fingerprint, "index_version": knowledge_base.INDEX_VERSION,
            "embedding_model": get_settings().embedding_model, "chunk_size": CHUNK_SIZE, "chunk_overlap": CHUNK_OVERLAP,
            "ocr": ocr.signature()}


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
            if read_files(collection, path) is None:  # index built by an older Hapilot: list its documents
                _write_files(collection, path, signature["fingerprint"], _bootstrap(collection, path, entries, saved, True))
            print(f"✅ Korpus bez zmian ({len(entries)} plików) — pomijam ponowne indeksowanie.")
            return saved
    if not entries:
        print(f"⚠️ Brak dokumentów ({', '.join(SUPPORTED)}) w {docs_dir}")
    if signature["ocr"] == "unavailable":
        print("⚠️ OCR niedostępny (brak Dockera lub obrazu OCR — `scripts/hapilotctl.py setup`): strony zeskanowane "
              "nie zostaną odczytane. Po zbudowaniu obrazu następne indeksowanie je odczyta.")

    facts: dict = {}
    chunks, skipped = split_entries(entries, facts)
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
    _write_files(collection, path, signature["fingerprint"],
                 {rel: {**_disk_facts(p), **facts[rel], "indexed_at": _now()} for p, rel in entries})
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
    with knowledge_base.index_lock:  # the panel's folder re-check must not pick up a half-added file
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
        facts: dict = {}
        chunks, skipped = split_entries(entries, facts)
        for item in skipped:  # an unreadable file is not kept
            (added_dir / Path(item["file"]).name).unlink(missing_ok=True)
        _add_chunks(knowledge_base.vector_store(collection=collection, path=path), chunks)
        knowledge_base.reset_cache()

        skipped_names = {Path(i["file"]).name for i in skipped}
        kept = [(p, rel) for p, rel in entries if p.name not in skipped_names]
        listed = current_files(collection, path, docs_dir, added_dir)
        listed.update({rel: {**_disk_facts(p), **facts[rel], "indexed_at": _now()} for p, rel in kept})
        _save_state(collection, path, docs_dir, added_dir, listed)
    return {"added": [rel for _p, rel in kept], "chunks": len(chunks), "skipped": skipped, "already": already}


# --------------------------------------------------------------------------- single documents (agent panel)

def index_entries(entries: list[tuple[Path, str]], *, collection: str, docs_dir: Path, added_dir: Optional[Path],
                  path: Optional[Path] = None) -> dict[str, dict]:
    """Index these (file, source path) pairs again: a document's old chunks are replaced by its current ones.
    Text extraction (OCR included) runs before the index is touched, so searches keep finding the old version
    meanwhile. Returns the per-file facts (``error`` set for a file that could not be read; it stays listed)."""
    from src import knowledge_base
    from src.config import get_settings

    path = Path(path or get_settings().chroma_path)
    disk = {rel: _disk_facts(p) for p, rel in entries}  # hashed before reading: a later change is seen again
    facts: dict = {}
    chunks, _skipped = split_entries(entries, facts)
    with knowledge_base.index_lock:
        listed = current_files(collection, path, docs_dir, added_dir)
        for _p, rel in entries:
            knowledge_base.delete_source(rel, collection, path)
        _add_chunks(knowledge_base.vector_store(collection=collection, path=path), chunks)
        for _p, rel in entries:
            listed[rel] = {**disk[rel], **facts[rel], "indexed_at": _now()}
        _save_state(collection, path, docs_dir, added_dir, listed)
    return {rel: listed[rel] for _p, rel in entries}


def remove_sources(sources: list[str], *, collection: str, docs_dir: Path, added_dir: Optional[Path],
                   path: Optional[Path] = None) -> None:
    """Remove documents (by source path) from the index — after their files were deleted or moved away."""
    from src import knowledge_base
    from src.config import get_settings

    path = Path(path or get_settings().chroma_path)
    with knowledge_base.index_lock:
        listed = current_files(collection, path, docs_dir, added_dir)
        for source in sources:
            knowledge_base.delete_source(source, collection, path)
            listed.pop(source, None)
        _save_state(collection, path, docs_dir, added_dir, listed)


# --------------------------------------------------------------------------- per-document list

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _disk_facts(path: Path) -> dict:
    stat = path.stat()
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": stat.st_size, "mtime": stat.st_mtime}


def files_path(collection: str, path: Path) -> Path:
    return Path(path) / f"{collection}.files.json"


def read_files(collection: str, path: Path) -> Optional[dict[str, dict]]:
    """Per-document facts of the index, or None when the list is missing or belongs to another fingerprint."""
    try:
        data = json.loads(files_path(collection, path).read_text(encoding="utf-8"))
        marker = json.loads((Path(path) / f"{collection}.fingerprint.json").read_text())
    except (OSError, ValueError):
        return None
    if data.get("fingerprint") != marker.get("fingerprint"):
        return None
    return data.get("files") or {}


def _write_files(collection: str, path: Path, fingerprint: str, files: dict) -> None:
    target = files_path(collection, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fingerprint": fingerprint, "files": dict(sorted(files.items()))}, indent=1,
                              ensure_ascii=False), encoding="utf-8")
    tmp.replace(target)


def current_files(collection: str, path: Path, docs_dir: Path, added_dir: Optional[Path]) -> dict[str, dict]:
    """The per-document list; rebuilt from the index when it is missing or out of step."""
    listed = read_files(collection, path)
    if listed is not None:
        return listed
    marker = Path(path) / f"{collection}.fingerprint.json"
    try:
        saved = json.loads(marker.read_text())
    except (OSError, ValueError):
        saved = {}
    entries = corpus_entries(docs_dir, added_dir)
    trusted = bool(saved) and saved.get("fingerprint") == _fingerprint(entries)
    listed = _bootstrap(collection, path, entries, saved, trusted)
    if trusted:  # the index matches the folders: keep the list
        _write_files(collection, path, saved["fingerprint"], listed)
    return listed


def _bootstrap(collection: str, path: Path, entries: list[tuple[Path, str]], saved: dict,
               trusted: bool) -> dict[str, dict]:
    """The per-document list of an index built without one. Content hashes are filled in only when the index
    matches the folders (``trusted``: same fingerprint); otherwise they stay empty, so the next folder re-check
    indexes every document again. Documents in the index but no longer on disk are listed too, so that it removes
    them."""
    from src import knowledge_base

    summary = knowledge_base.source_summary(collection, path)
    reasons = {s.get("file"): s.get("reason") for s in saved.get("skipped") or []}
    unknown = {"sha256": None, "size": None, "mtime": None}
    listed: dict[str, dict] = {}
    for p, rel in entries:
        in_index = summary.get(rel)
        if in_index is None and not (trusted and rel in reasons):
            continue  # not in the index: the re-check indexes it
        listed[rel] = {**(_disk_facts(p) if trusted else unknown), "chunks": in_index["chunks"] if in_index else 0,
                       "pages": page_count(p), "sheets": None,
                       "ocr_pages": len(in_index["ocr_pages"]) if in_index else 0, "ocr_confidence": None,
                       "error": None if in_index else reasons.get(rel), "indexed_at": None}
    on_disk = {rel for _p, rel in entries}
    for source, in_index in summary.items():
        if source not in on_disk:
            listed[source] = {**unknown, "chunks": in_index["chunks"], "pages": None, "sheets": None,
                              "ocr_pages": len(in_index["ocr_pages"]), "ocr_confidence": None, "error": None,
                              "indexed_at": None}
    return listed


def _save_state(collection: str, path: Path, docs_dir: Path, added_dir: Optional[Path], listed: dict) -> None:
    """Write the fingerprint marker and the per-document list together, for the folders as they are now."""
    entries = corpus_entries(docs_dir, added_dir)
    signature = _signature(_fingerprint(entries))
    marker = Path(path) / f"{collection}.fingerprint.json"
    try:
        stats = json.loads(marker.read_text())
    except (OSError, ValueError):
        stats = {"collection": collection, "docs_dir": str(docs_dir)}
    stats.update(signature, files=len(entries), chunks=sum(int(f.get("chunks") or 0) for f in listed.values()),
                 skipped=[{"file": rel, "reason": f["error"]} for rel, f in sorted(listed.items()) if f.get("error")])
    Path(path).mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(stats, indent=2, ensure_ascii=False))
    _write_files(collection, path, signature["fingerprint"], listed)


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
