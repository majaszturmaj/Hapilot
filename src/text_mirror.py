"""Text mirror of the knowledge base, for the v3 agent: one UTF-8 .txt per indexed document and one CSV per sheet of
a spreadsheet, read and searched with grep in the agent's container (read-only at /praca/tekst, src/agent_workspace.py).

The mirror is written from the same extraction as the index, OCR included (src/extractors.py, src/ocr.py), and is
kept in step with the index's per-document list, ``<collection>.files.json`` (the list the "📚 Baza wiedzy" tab
shows): ``ingest`` writes it whole; ``add_documents``, ``index_entries`` and ``remove_sources`` change single
documents (src/ingest_data.py); ``sync`` writes what is missing or stale and removes what is no longer listed (an
index built before the mirror existed, a mirror deleted by hand) without touching the index. The panel's folder
re-check runs ``sync`` too. The index never reads the mirror, so a mirror failure is reported and indexing goes on.

Layout, next to the index (the Chroma folder):

    <collection>.tekst/<source path>.txt          e.g. umowy/umowa.pdf.txt, dodane/oferta.docx.txt
    <collection>.tekst/<source path>.<sheet>.csv  one per non-empty sheet of a .xlsx
    <collection>.tekst.json                       per document: content hash of the original, files written

What a .txt holds:
- PDF: every page starts with a line "[strona N]"; "[strona N — OCR, pewność 87%]" for a page read by OCR and
  "[strona N — brak tekstu]" for a page without text;
- XLSX: every sheet starts with "[arkusz NAME]", rows as in the index (cells joined by " | "); the CSV of a sheet
  starts at cell A1 and has every row the same length, so line N is row N of the sheet;
- DOCX: the text the index reads, plus the header ("[nagłówek]") and footer ("[stopka]");
- EML: the text the index reads, plus "[treść HTML wiadomości]" when the HTML body has words the plain one lacks;
- CSV: the whole file (the index reads at most ``CSV_ROWS`` rows); TXT, MD: the text;
- a document that could not be read: one line with the reason.

    python -m src.text_mirror check [--chunks]   # does the mirror match the index's document list? (exit 1 if not)
    python -m src.text_mirror sync               # bring it in step
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, NamedTuple, Optional, Union

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.extractors import (ExtractionError, Segment, _text, as_text, docx_header_footer, eml_html_extra, extract,
                            xlsx_tables)

MIRROR_VERSION = 1  # bump when what is written changes: ``sync`` then rewrites every document

Extracted = Union[list[Segment], str]  # what ``extract`` gave: segments, or why the document could not be read

_lock = threading.Lock()  # one writer of the mirror manifest at a time in this process


class Doc(NamedTuple):
    path: Path                            # the original
    source: str                           # its source path in the index, e.g. "umowy/umowa.pdf" or "dodane/x.docx"
    sha256: Optional[str]                 # content hash of the original, as the index lists it
    extracted: Optional[Extracted] = None  # the indexing run's extraction (None: extract here)


@dataclass
class Rendered:
    files: dict[str, str]  # suffix added to the document's name (".txt", ".<sheet>.csv") -> content
    pages: Optional[int] = None
    ocr_pages: list[int] = field(default_factory=list)
    sheets: list[str] = field(default_factory=list)
    error: Optional[str] = None


def mirror_dir(index_path: Path, collection: str) -> Path:
    return Path(index_path) / f"{collection}.tekst"


def manifest_path(index_path: Path, collection: str) -> Path:
    return Path(index_path) / f"{collection}.tekst.json"


# --------------------------------------------------------------------------- rendering

def render(path: Path, extracted: Optional[Extracted] = None) -> Rendered:
    """The mirror files of one document. ``extracted``: what ``extract`` gave for it in the indexing run (so a
    document is read once); when None it is extracted here (OCR results come from the OCR cache)."""
    if extracted is None:
        try:
            extracted = extract(path)
        except ExtractionError as exc:
            extracted = str(exc)
    if isinstance(extracted, str):
        return Rendered({".txt": _clean(f"[nie udało się odczytać tekstu dokumentu: {extracted}]")}, error=extracted)
    suffix = path.suffix.lower()
    data = path.read_bytes()
    out = Rendered({})
    csvs: dict[str, str] = {}
    if suffix == ".pdf":
        out.pages = _pdf_pages(data)
        out.ocr_pages = [s.meta["page"] for s in extracted if s.meta.get("ocr")]
        text = _pdf_text(extracted, out.pages)
    elif suffix == ".docx":
        headers, footers = _extra(docx_header_footer, data, path, ([], []))
        text = "\n\n".join([f"[nagłówek]\n{h}" for h in headers] + [as_text(extracted)]
                           + [f"[stopka]\n{f}" for f in footers])
    elif suffix == ".eml":
        html = _extra(eml_html_extra, data, path, "")
        text = as_text(extracted) + (f"\n\n[treść HTML wiadomości]\n{html}" if html else "")
    elif suffix == ".xlsx":
        text = as_text(extracted)
        out.sheets = [s.meta["sheet"] for s in extracted if "sheet" in s.meta]
        for title, rows in _extra(xlsx_tables, data, path, []):
            csvs[_csv_suffix(title, csvs)] = _csv_text(rows)
    elif suffix == ".csv":
        text = _text(data)  # the whole file; the index reads only its first rows
    else:
        text = as_text(extracted)
    out.files = {".txt": _clean(text), **csvs}
    return out


def _extra(read, data: bytes, path: Path, default):
    """Text the index does not read (header and footer, HTML body, CSV): a failure leaves it out, with a warning."""
    try:
        return read(data)
    except Exception as exc:
        print(f"⚠️ Kopia tekstowa {path.name}: pominięto {read.__name__} ({type(exc).__name__}: {exc})", flush=True)
        return default


def _pdf_pages(data: bytes) -> Optional[int]:
    try:
        from pypdf import PdfReader

        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return None


def _pdf_text(segments: list[Segment], pages: Optional[int]) -> str:
    by_page = {s.meta.get("page"): s for s in segments}
    numbers = range(1, pages + 1) if pages else sorted(n for n in by_page if n)
    blocks = []
    for n in numbers:
        seg = by_page.get(n)
        if seg is None:
            blocks.append(f"[strona {n} — brak tekstu]")
        elif seg.meta.get("ocr"):
            blocks.append(f"[strona {n} — OCR, pewność {float(seg.meta.get('ocr_confidence') or 0):.0f}%]\n{seg.text}")
        else:
            blocks.append(f"[strona {n}]\n{seg.text}")
    return "\n\n".join(blocks)


def _csv_suffix(title: str, taken: dict) -> str:
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title).strip() or "arkusz"
    suffix, n = f".{base}.csv", 2
    while suffix in taken:
        suffix, n = f".{base}_{n}.csv", n + 1
    return suffix


def _csv_text(rows: list[list[str]]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


def _clean(text: str) -> str:
    """Unix line ends, no NUL bytes (grep would take the file for binary), one final newline."""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").rstrip() + "\n"


# --------------------------------------------------------------------------- writing

def write(index_path: Path, collection: str, docs: Iterable[Doc], *, remove: Iterable[str] = (),
          exclusive: bool = False) -> dict:
    """Write the mirror files of ``docs``, drop the documents in ``remove`` and, with ``exclusive`` (a full rebuild),
    every document not in ``docs``. Returns {"written": n, "removed": n}."""
    docs = list(docs)
    rendered = [(doc, render_safely(doc)) for doc in docs if _safe_source(doc.source)]
    return _apply(index_path, collection, rendered, remove=remove,
                  keep_only={doc.source for doc in docs} if exclusive else None)


def render_safely(doc: Doc) -> Rendered:
    try:
        return render(doc.path, doc.extracted)
    except Exception as exc:  # e.g. the file went away after indexing: the mirror says so
        reason = f"{type(exc).__name__}: {exc}"
        return Rendered({".txt": _clean(f"[nie udało się zapisać tekstu dokumentu: {reason}]")}, error=reason)


def _apply(index_path: Path, collection: str, rendered: list[tuple[Doc, Rendered]], *, remove: Iterable[str] = (),
           keep_only: Optional[set[str]] = None, clean_orphans: bool = False) -> dict:
    root = mirror_dir(index_path, collection)
    with _lock:
        manifest = read_manifest(index_path, collection)
        files = manifest["files"]
        dropped = set(remove) | ({s for s in files if s not in keep_only} if keep_only is not None else set())
        removed = 0
        for source in sorted(dropped):
            entry = files.pop(source, None)
            if entry is not None:
                removed += 1
                for name in entry.get("outputs") or []:
                    _drop(root, name)
        for doc, out in rendered:
            old = set((files.get(doc.source) or {}).get("outputs") or [])
            outputs = save(root, doc.source, out)
            for name in old - set(outputs):
                _drop(root, name)
            files[doc.source] = {"version": MIRROR_VERSION, "sha256": doc.sha256, "outputs": outputs,
                                 "pages": out.pages, "ocr_pages": out.ocr_pages, "sheets": out.sheets,
                                 "error": out.error, "written_at": _now()}
        if clean_orphans:
            removed_files = _remove_orphans(root, files)
        _write_manifest(index_path, collection, files)
    result = {"written": len(rendered), "removed": removed}
    if clean_orphans:
        result["orphans_removed"] = removed_files
    return result


def save(folder: Path, name: str, rendered: Rendered) -> list[str]:
    """Write the rendered files of the document ``name`` (a path relative to ``folder``) as ``name`` + suffix, each
    atomically; returns the paths written, relative to ``folder``. Also used for the v3 agent's uploads."""
    written = []
    for suffix, content in rendered.files.items():
        _put(folder, name + suffix, content)
        written.append(name + suffix)
    return written


def _safe_source(source: str) -> bool:
    """A source path that stays inside the mirror (relative, no "." or ".." parts)."""
    p = PurePosixPath(source)
    return bool(source) and not p.is_absolute() and "\\" not in source and \
        all(part not in ("", ".", "..") for part in source.split("/"))


def _inside(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _put(root: Path, name: str, content: str) -> None:
    target = root / name
    if not _safe_source(name) or not _inside(root, target):
        raise ValueError(f"mirror path outside the mirror: {name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    tmp.write_bytes(content.encode("utf-8", errors="replace"))
    os.replace(tmp, target)


def _drop(root: Path, name: str) -> None:
    if not _safe_source(name):
        return
    target = root / name
    if target.is_symlink() or not _inside(root, target):
        return
    target.unlink(missing_ok=True)
    _prune(root, target.parent)


def _prune(root: Path, folder: Path) -> None:
    """Remove empty folders from ``folder`` up to (not including) the mirror root."""
    root = root.resolve()
    folder = folder.resolve()
    while folder != root and root in folder.parents:
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


def _files_on_disk(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() or p.is_symlink()}


def _remove_orphans(root: Path, files: dict) -> int:
    expected = {name for entry in files.values() for name in entry.get("outputs") or []}
    removed = 0
    for name in sorted(_files_on_disk(root) - expected):
        target = root / name
        if target.is_symlink() or _inside(root, target):
            target.unlink(missing_ok=True)
            _prune(root, target.parent)
            removed += 1
    return removed


def read_manifest(index_path: Path, collection: str) -> dict:
    try:
        data = json.loads(manifest_path(index_path, collection).read_text(encoding="utf-8"))
        files = data.get("files")
        if isinstance(files, dict):
            return {"files": files}
    except (OSError, ValueError):
        pass
    return {"files": {}}


def _write_manifest(index_path: Path, collection: str, files: dict) -> None:
    target = manifest_path(index_path, collection)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps({"collection": collection, "files": dict(sorted(files.items()))}, indent=1,
                              ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- keeping in step with the index

def _in_step(entry: Optional[dict], facts: dict, root: Path) -> bool:
    return bool(entry) and entry.get("version") == MIRROR_VERSION and bool(facts.get("sha256")) \
        and entry.get("sha256") == facts.get("sha256") \
        and all((root / name).is_file() for name in entry.get("outputs") or [])


def sync(index_path: Path, collection: str, docs_dir: Path, added_dir: Optional[Path]) -> dict:
    """Bring the mirror in step with the index's document list: write documents whose mirror is missing, stale
    (another content hash, an older mirror version) or incomplete; remove documents no longer listed and files no
    document owns. A document changed on disk since it was indexed is left to the re-indexing (which writes its
    mirror); the index itself is not touched."""
    from src import ingest_data, knowledge_base

    index_path = Path(index_path)
    if not (index_path / f"{collection}.fingerprint.json").is_file():
        return {"written": 0, "removed": 0, "pending": [], "originals_missing": [], "note": "no index yet"}
    root = mirror_dir(index_path, collection)
    with knowledge_base.index_lock:
        listed = ingest_data.current_files(collection, index_path, docs_dir, added_dir)
        files = read_manifest(index_path, collection)["files"]
    on_disk = {rel: p for p, rel in ingest_data.corpus_entries(docs_dir, added_dir)}
    todo, pending, originals_missing = [], [], []
    for source, facts in sorted(listed.items()):
        if not _safe_source(source) or _in_step(files.get(source), facts, root):
            continue
        path = on_disk.get(source)
        if path is None:
            originals_missing.append(source)
            continue
        sha = _sha256(path)
        if sha != facts.get("sha256"):  # changed since indexing (or not hashed by the index): re-indexing writes it
            pending.append(source)
            continue
        todo.append(Doc(path, source, sha))
    rendered = [(doc, render_safely(doc)) for doc in todo]  # outside the index lock: extraction can take a while
    with knowledge_base.index_lock:
        listed = ingest_data.read_files(collection, index_path)
        if listed is None:  # no trustworthy list (yet): the folder re-check re-indexes, and that writes the mirror
            return {"written": 0, "removed": 0, "pending": pending, "originals_missing": originals_missing,
                    "note": "the index has no per-document list matching its fingerprint"}
        fresh = [(doc, out) for doc, out in rendered if (listed.get(doc.source) or {}).get("sha256") == doc.sha256]
        result = _apply(index_path, collection, fresh, keep_only=set(listed), clean_orphans=True)
    result.update(pending=pending, originals_missing=originals_missing)
    return result


def check(index_path: Path, collection: str, *, chunks: bool = False) -> dict:
    """Compare the mirror with the index's document list (and, with ``chunks``, every chunk of the index with the
    text of its document in the mirror). ``ok`` when every listed document has its mirror, written from the content
    the index holds, every file the manifest names exists, and nothing else is there."""
    from src import ingest_data

    listed = ingest_data.read_files(collection, Path(index_path))
    if listed is None:
        return {"ok": False, "problem": "the index has no per-document list matching its fingerprint (run ingest)"}
    root = mirror_dir(index_path, collection)
    files = read_manifest(index_path, collection)["files"]
    both = sorted(set(listed) & set(files))
    expected = {name for entry in files.values() for name in entry.get("outputs") or []}
    on_disk = _files_on_disk(root)
    report = {
        "documents": len(listed),
        "mirrored": len(both),
        "unreadable": sorted(s for s, f in listed.items() if f.get("error")),
        "outputs": len(expected),
        "csv": sum(1 for name in expected if name.endswith(".csv")),
        "missing": sorted(set(listed) - set(files)),
        "extra": sorted(set(files) - set(listed)),
        "stale": [s for s in both if files[s].get("version") != MIRROR_VERSION
                  or not listed[s].get("sha256") or files[s].get("sha256") != listed[s].get("sha256")],
        "error_mismatch": [s for s in both if bool(files[s].get("error")) != bool(listed[s].get("error"))],
        "missing_files": sorted(expected - on_disk),
        "orphans": sorted(on_disk - expected),
    }
    problems = ("missing", "extra", "stale", "error_mismatch", "missing_files", "orphans")
    if chunks:
        report["chunks"] = _check_chunks(index_path, collection, root, files)
    report["ok"] = not any(report[k] for k in problems) and not (chunks and report["chunks"]["not_found"])
    return report


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower())


def _check_chunks(index_path: Path, collection: str, root: Path, files: dict) -> dict:
    """Every chunk of the index must occur in the mirror text of its document (letters and digits compared, so a
    CSV's " | "-joined rows match the raw file)."""
    from src import knowledge_base

    found = knowledge_base._collection(collection, Path(index_path))
    data = found.get(include=["documents", "metadatas"]) if found is not None else {"documents": [], "metadatas": []}
    texts: dict[str, Optional[str]] = {}
    total, not_found = 0, []
    for text, meta in zip(data.get("documents") or [], data.get("metadatas") or []):
        meta = meta or {}
        source = str(meta.get("source", ""))
        if source not in texts:
            outputs = (files.get(source) or {}).get("outputs") or []
            txt = root / outputs[0] if outputs and _safe_source(outputs[0]) else None
            texts[source] = _norm(txt.read_text(encoding="utf-8")) if txt is not None and txt.is_file() else None
        piece = text or ""
        if piece.startswith("[kolumny: ") and "]\n" in piece:  # spreadsheet chunks repeat the column names
            piece = piece.split("]\n", 1)[1]
        piece = re.sub(r"\n?\[… pokazano \d+ z \d+ wierszy[^\]]*\]\s*$", "", piece)  # the CSV row-limit note
        total += 1
        if texts[source] is None or _norm(piece) not in texts[source]:
            not_found.append({"source": source, "page": meta.get("page"), "sheet": meta.get("sheet"),
                              "chunk_id": meta.get("chunk_id")})
    return {"checked": total, "not_found": not_found}


# --------------------------------------------------------------------------- command line

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("check", "sync"))
    parser.add_argument("--collection", help="collection name (default: HAPILOT_COLLECTION)")
    parser.add_argument("--index", type=Path, help="index folder (default: HAPILOT_CHROMA_DIR)")
    parser.add_argument("--docs", type=Path, help="documents folder (default: HAPILOT_DOCS_DIR)")
    parser.add_argument("--chunks", action="store_true", help="check: also find every chunk of the index in the mirror")
    args = parser.parse_args(argv)

    from src.privacy import apply_offline_env

    apply_offline_env()
    from src.config import get_settings

    s = get_settings()
    index_path, collection = (args.index or s.chroma_path), (args.collection or s.collection)
    if args.command == "sync":
        result = sync(index_path, collection, (args.docs or s.docs_dir).resolve(), s.added_docs_dir)
        print(json.dumps(result, indent=1, ensure_ascii=False))
        return 0
    report = check(index_path, collection, chunks=args.chunks)
    print(json.dumps(report, indent=1, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
