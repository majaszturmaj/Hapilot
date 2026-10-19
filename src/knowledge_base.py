"""Company knowledge base: embedded ChromaDB + hybrid retrieval (E5 vectors and BM25, fused by RRF).

- ChromaDB runs inside the Hapilot process (``PersistentClient``): no database container or port;
  its anonymised telemetry is off.
- Embeddings: ``intfloat/multilingual-e5-base`` on CPU, with the E5 prefixes ("passage: " for
  documents, "query: " for questions).
- Search: the dense ranking and a BM25 ranking (lower case, Polish stop words, 6-character prefix
  stemming) are fused by reciprocal rank fusion. Several collections can be searched at once (the
  corpus plus a task's attachments).
- Which collections are searched comes from the run context (``kb_path``, ``kb_collection``,
  ``extra_collections``); the default is the UI's knowledge base from Settings.

Heavy libraries are imported only when the knowledge base is used; tests replace the backend.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol

from src.config import get_settings

INDEX_VERSION = 2  # bump when chunking or embedding changes: an existing index is then rebuilt
RRF_K = 60


@dataclass
class Chunk:
    file: str
    chunk_id: str
    score: float
    text: str
    page: Optional[int] = None
    sheet: Optional[str] = None
    score_dense: Optional[float] = None
    score_bm25: Optional[float] = None
    rank: int = 0
    collection: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- embeddings and Chroma

class E5Embeddings:
    """Wraps a sentence-transformers embedding model and adds the E5 prefixes."""

    def __init__(self, model_name: str):
        from langchain_huggingface import HuggingFaceEmbeddings

        self._inner = HuggingFaceEmbeddings(model_name=model_name, model_kwargs={"device": "cpu"},
                                            encode_kwargs={"normalize_embeddings": True})
        self._e5 = "e5" in model_name.lower()

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._inner.embed_documents([f"passage: {t}" if self._e5 else t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._inner.embed_query(f"query: {text}" if self._e5 and not text.startswith("query:") else text)


_lock = threading.RLock()
_embeddings: Any = None
_clients: dict[str, Any] = {}
_stores: dict[tuple[str, str], Any] = {}


def embeddings() -> Any:
    global _embeddings
    with _lock:
        if _embeddings is None:
            _embeddings = E5Embeddings(get_settings().embedding_model)
        return _embeddings


def chroma_client(path: Optional[Path] = None) -> Any:
    import chromadb
    from chromadb.config import Settings as ChromaSettings

    path = Path(path or get_settings().chroma_path)
    with _lock:
        if str(path) not in _clients:
            path.mkdir(parents=True, exist_ok=True)
            _clients[str(path)] = chromadb.PersistentClient(path=str(path), settings=ChromaSettings(anonymized_telemetry=False))
        return _clients[str(path)]


def vector_store(collection: Optional[str] = None, path: Optional[Path] = None) -> Any:
    from langchain_chroma import Chroma

    path = Path(path or get_settings().chroma_path)
    collection = collection or get_settings().collection
    with _lock:
        key = (str(path), collection)
        if key not in _stores:
            _stores[key] = Chroma(client=chroma_client(path), collection_name=collection, embedding_function=embeddings())
        return _stores[key]


def reset_cache() -> None:
    with _lock:
        _stores.clear()
        _bm25_cache.clear()


# --------------------------------------------------------------------------- BM25

STOPWORDS = set("""a aby ale albo ani bo by być był była było były co czy dla do gdy gdzie i ich im ja jak jako je jego jej
jest jeśli już każdy kiedy kto która które który lub ma może mu na nad nie nich niż o od oraz po pod przez przy się są
ta tak także te ten tej tego to tu tym tylko u w we więc z za ze że""".split())
_TOKEN = re.compile(r"\w+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    tokens = []
    for raw in _TOKEN.findall(text.lower()):
        if raw in STOPWORDS or (len(raw) < 2 and not raw.isdigit()):
            continue
        tokens.append(raw[:6] if raw.isalpha() else raw)
    return tokens


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.docs = [Counter(d) for d in docs]
        self.lengths = [len(d) for d in docs]
        self.avg = (sum(self.lengths) / len(docs)) if docs else 0.0
        df: Counter = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: list[str]) -> list[float]:
        out = []
        for doc, length in zip(self.docs, self.lengths):
            s = 0.0
            for term in query:
                tf = doc.get(term)
                if tf:
                    s += self.idf[term] * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * length / (self.avg or 1)))
            out.append(s)
        return out


_bm25_cache: dict[tuple[str, str], tuple[int, list, BM25]] = {}


# --------------------------------------------------------------------------- backend (Chroma, or a test double)

class Backend(Protocol):
    def dense(self, path: Path, collection: str, query: str, k: int) -> list[tuple[str, str, dict, float]]: ...
    def all_chunks(self, path: Path, collection: str) -> list[tuple[str, str, dict]]: ...


class ChromaBackend:
    def dense(self, path, collection, query, k):
        pairs = vector_store(collection, path).similarity_search_with_relevance_scores(query, k=k)
        return [(str(d.metadata.get("chunk_id", "")), d.page_content, d.metadata or {}, float(s)) for d, s in pairs]

    def all_chunks(self, path, collection):
        data = chroma_client(path).get_collection(collection).get(include=["documents", "metadatas"])
        return list(zip(data["ids"], data["documents"], data["metadatas"]))


_backend: Backend = ChromaBackend()


def set_backend(backend: Optional[Backend]) -> None:
    global _backend
    _backend = backend or ChromaBackend()
    reset_cache()


def _dense_fresh(path: Path, collection: str, query: str, k: int) -> list:
    """Dense search; if the collection was rebuilt by another process (``ingest --force`` while the UI runs),
    the cached handle points at a deleted collection — reload it once and retry."""
    try:
        return _backend.dense(path, collection, query, k)
    except Exception as exc:
        if "does not exist" not in str(exc):
            raise
        print(f"🔄 [RAG] {collection}: indeks przebudowany w międzyczasie — wczytuję go ponownie")
        reset_cache()
        return _backend.dense(path, collection, query, k)


def _bm25_for(path: Path, collection: str) -> tuple[list, BM25]:
    chunks = _backend.all_chunks(path, collection)
    key = (str(path), collection)
    with _lock:
        cached = _bm25_cache.get(key)
        if cached and cached[0] == len(chunks):
            return cached[1], cached[2]
        index = BM25([tokenize(text) for _id, text, _meta in chunks])
        _bm25_cache[key] = (len(chunks), chunks, index)
        return chunks, index


# --------------------------------------------------------------------------- search

def search_targets() -> list[tuple[Path, str]]:
    """Collections to search in the current run (corpus first, then attachment collections)."""
    from src import run_context

    settings = get_settings()
    targets = [(Path(run_context.current("kb_path") or settings.chroma_path),
                run_context.current("kb_collection") or settings.collection)]
    for path, name in run_context.current("extra_collections") or []:
        targets.append((Path(path), name))
    return targets


def search(query: str, k: Optional[int] = None, targets: Optional[list[tuple[Path, str]]] = None) -> list[Chunk]:
    """Top-k chunks by reciprocal rank fusion of dense and BM25 rankings over all target collections."""
    from src import tracing

    k = k or get_settings().rag_top_k
    fetch = max(20, 3 * k)
    targets = targets or search_targets()
    fused: dict[tuple[str, str], dict] = {}
    q_tokens = tokenize(query)

    for path, collection in targets:
        try:
            dense = _dense_fresh(path, collection, query, fetch)
        except Exception as exc:  # e.g. a collection that does not exist
            print(f"⚠️ [RAG] {collection}: {exc}")
            continue
        for rank, (cid, text, meta, score) in enumerate(dense, 1):
            entry = fused.setdefault((collection, cid), {"text": text, "meta": meta, "rrf": 0.0})
            entry["rrf"] += 1 / (RRF_K + rank)
            entry["dense"] = round(score, 4)
        chunks, index = _bm25_for(path, collection)
        scores = index.scores(q_tokens)
        ranked = sorted((i for i, s in enumerate(scores) if s > 0), key=lambda i: scores[i], reverse=True)[:fetch]
        for rank, i in enumerate(ranked, 1):
            cid, text, meta = chunks[i]
            entry = fused.setdefault((collection, cid), {"text": text, "meta": meta, "rrf": 0.0})
            entry["rrf"] += 1 / (RRF_K + rank)
            entry["bm25"] = round(scores[i], 4)

    best = sorted(fused.items(), key=lambda kv: kv[1]["rrf"], reverse=True)[:k]
    result = []
    for rank, ((collection, cid), e) in enumerate(best, 1):
        meta = e["meta"] or {}
        result.append(Chunk(file=str(meta.get("source", "")), chunk_id=cid, score=round(e["rrf"], 6), text=e["text"],
                            page=meta.get("page"), sheet=meta.get("sheet"), score_dense=e.get("dense"),
                            score_bm25=e.get("bm25"), rank=rank, collection=collection))
    tracing.event("retrieval", query=query, method="hybrid", k=k, collections=[c for _p, c in targets],
                  chunks=[c.to_dict() for c in result])
    return result


def format_chunks(chunks: Iterable[Chunk]) -> str:
    parts = []
    for c in chunks:
        where = f", str. {c.page}" if c.page else (f", arkusz {c.sheet}" if c.sheet else "")
        parts.append(f"--- ŹRÓDŁO: {c.file}{where} ---\n{c.text}")
    return "\n".join(parts) if parts else "Brak wyników."


def chunk_id(source: str, index: int, text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return f"{source}#{index}:{digest}"
