"""Company knowledge base: ChromaDB (embedded, on local disk) + multilingual E5 embeddings on CPU.

ChromaDB runs inside the Hapilot process (``PersistentClient``), so there is no database
container or network port, and its anonymised telemetry is switched off. Heavy libraries
(torch, sentence-transformers, chromadb) are imported only when the knowledge base is used.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from src.config import get_settings


@dataclass
class Chunk:
    file: str
    chunk_id: str
    score: float
    text: str


_lock = threading.Lock()
_store: Any = None


def _embeddings() -> Any:
    from langchain_huggingface import HuggingFaceEmbeddings

    return HuggingFaceEmbeddings(
        model_name=get_settings().embedding_model,
        model_kwargs={"device": "cpu"},  # the GPU is reserved for the language model
        encode_kwargs={"normalize_embeddings": True},
    )


def chroma_client(path: Optional[Path] = None) -> Any:
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    path = path or get_settings().chroma_path
    path.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(path), settings=ChromaSettings(anonymized_telemetry=False))


def vector_store(collection: Optional[str] = None, path: Optional[Path] = None) -> Any:
    """The LangChain Chroma store for a collection (cached for the default one)."""
    global _store
    from langchain_chroma import Chroma

    if collection is None and path is None:
        with _lock:
            if _store is None:
                _store = Chroma(client=chroma_client(), collection_name=get_settings().collection,
                                embedding_function=_embeddings())
            return _store
    return Chroma(client=chroma_client(path), collection_name=collection or get_settings().collection,
                  embedding_function=_embeddings())


def reset_cache() -> None:
    global _store
    with _lock:
        _store = None


def _e5_query(query: str) -> str:
    model = get_settings().embedding_model.lower()
    return f"query: {query}" if "e5" in model and not query.startswith("query:") else query


def search(query: str, k: Optional[int] = None, store: Any = None) -> list[Chunk]:
    """Top-k chunks for a query, best first (score = cosine similarity in [0, 1] when available)."""
    store = store or vector_store()
    k = k or get_settings().rag_top_k
    pairs = store.similarity_search_with_relevance_scores(_e5_query(query), k=k)
    chunks = []
    for doc, score in pairs:
        meta = doc.metadata or {}
        chunks.append(Chunk(file=Path(meta.get("source", "")).name, chunk_id=str(meta.get("chunk_id", "")),
                            score=round(float(score), 4), text=doc.page_content))
    return chunks


def format_chunks(chunks: Iterable[Chunk]) -> str:
    parts = [f"--- ŹRÓDŁO: {c.file} ---\n{c.text}" for c in chunks]
    return "\n".join(parts) if parts else "Brak wyników."


def chunk_id(source: str, index: int, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"{Path(source).name}#{index}:{digest}"
