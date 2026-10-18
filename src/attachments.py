"""Files attached to a request — the same code for UI uploads and evaluation tasks.

Each file is copied into the session workspace (so code in the sandbox can read it, e.g. an .xlsx)
and its text is extracted with src/extractors.py. If all attachments together have at most
``inline_limit`` characters, their text is added to the user's message as ``[Załącznik: name]``
blocks; otherwise they are indexed into a per-session collection that the knowledge search queries
together with the company documents, and the message lists them.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src import run_context, tracing
from src.extractors import ExtractionError, as_text, extract

INLINE_LIMIT = 20_000


@dataclass
class PreparedAttachments:
    files: list[dict] = field(default_factory=list)   # name, bytes, sha256, chars, error
    message_block: str = ""                            # text to append to the user's message
    collection: Optional[tuple[str, str]] = None       # (chroma path, collection) when indexed

    @property
    def names(self) -> list[str]:
        return [f["name"] for f in self.files]


def _safe_name(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE).strip("._") or "zalacznik"


def collection_name(session: str) -> str:
    """A Chroma-compatible collection name for a session's attachments."""
    digest = hashlib.sha256(session.encode("utf-8")).hexdigest()[:16]
    return f"att_{digest}"


def prepare(paths: list[Path], *, index_path: Path, inline_limit: int = INLINE_LIMIT,
            display_names: Optional[list[str]] = None) -> PreparedAttachments:
    """Copy, extract and inline or index attachments for the current session."""
    result = PreparedAttachments()
    if not paths:
        return result
    workspace = run_context.workspace_dir()
    texts: list[tuple[str, str]] = []
    for i, src in enumerate(paths):
        name = _safe_name((display_names or [])[i] if display_names and i < len(display_names) else src.name)
        target = workspace / name
        n = 1
        while target.exists():
            target = workspace / f"{Path(name).stem}_{n}{Path(name).suffix}"
            n += 1
        shutil.copyfile(src, target)
        data = target.read_bytes()
        info = {"name": target.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "chars": 0, "error": None}
        try:
            text = as_text(extract(target))
            info["chars"] = len(text)
            texts.append((target.name, text))
        except ExtractionError as exc:
            info["error"] = str(exc)
        result.files.append(info)

    listing = ", ".join(result.names)
    total = sum(len(t) for _n, t in texts)
    lines = ["", f"Załączone pliki (dostępne też w katalogu roboczym dla kodu Python): {listing}"]
    unreadable = [f["name"] for f in result.files if f["error"]]
    if unreadable:
        lines.append(f"Nie udało się odczytać tekstu z: {', '.join(unreadable)}")
    if total <= inline_limit:
        for name, text in texts:
            lines += ["", f"[Załącznik: {name}]", text]
    else:
        result.collection = (str(index_path), collection_name(run_context.session_id()))
        _index(texts, index_path, result.collection[1])
        lines.append("Treść załączników jest długa — przeszukuję ją razem z dokumentami firmy.")
    result.message_block = "\n".join(lines)
    tracing.event("attachments", files=result.files, inlined=result.collection is None,
                  collection=result.collection[1] if result.collection else None)
    return result


def _index(texts: list[tuple[str, str]], index_path: Path, collection: str) -> None:
    """Index attachment texts into their own collection (same chunking and embeddings as the corpus)."""
    from langchain_core.documents import Document
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    from src import knowledge_base
    from src.ingest_data import CHUNK_OVERLAP, CHUNK_SIZE

    splitter = RecursiveCharacterTextSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP,
                                              separators=["\n\n", "\n", ". ", " ", ""])
    docs = []
    for name, text in texts:
        for i, piece in enumerate(splitter.split_text(text)):
            source = f"załącznik:{name}"
            docs.append(Document(page_content=piece, metadata={"source": source, "chunk_id": knowledge_base.chunk_id(source, i, piece)}))
    client = knowledge_base.chroma_client(index_path)
    try:
        client.delete_collection(collection)
    except Exception:
        pass
    knowledge_base.reset_cache()
    store = knowledge_base.vector_store(collection=collection, path=index_path)
    for i in range(0, len(docs), 64):
        batch = docs[i:i + 64]
        store.add_documents(batch, ids=[d.metadata["chunk_id"] for d in batch])
