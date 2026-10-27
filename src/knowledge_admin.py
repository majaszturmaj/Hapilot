"""The company knowledge base in the agent panel (tab "📚 Baza wiedzy", administrators only; src/ui_admin.py).

What an administrator can do there:

- see every document the knowledge base searches — the company documents folder (``HAPILOT_DOCS_DIR``) and the
  documents users added from the chat (``HAPILOT_ADDED_DOCS_DIR``, source ``dodane/…``) — with type, size, pages,
  chunks, pages read by OCR, who added it and when, and whether it is indexed;
- add documents (several at once, up to ``HAPILOT_KB_MAX_UPLOAD_MB`` each): they are saved straight into the company
  documents folder and indexed in the background, OCR included;
- replace a document by a new version (same name and type) and delete one; the previous file goes to a trash
  (``<data dir>/knowledge_base/trash``) for ``HAPILOT_KB_TRASH_DAYS`` days and can be restored from there;
- read the text the index got from a document (per page, scans marked) and download or open the original.

Indexing runs in one background thread of the UI process (the embedded database has one writer): only the changed
documents, never a rebuild (src/ingest_data.py ``index_entries`` / ``remove_sources``). The same thread re-checks
the folders at start-up and every ``HAPILOT_KB_RESCAN_MINUTES`` minutes, so files copied into the folder on the
server, changed or deleted there are picked up without ``ingest`` (which refuses to run while the UI runs). Who
added what and when: ``<data dir>/knowledge_base/registry.json``; every change is in the audit log.

A documents folder inside the Hapilot checkout (the demo documents, the default) is shown but not changed: the
updater refuses to update a checkout whose tracked files were edited.

UI only: evaluation runs use their own fixed corpus and never load this module.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
import unicodedata
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from src.config import PROJECT_ROOT, get_settings
from src.extractors import SUPPORTED

ADDED_PREFIX = "dodane"   # source prefix of documents added from the chat (src/ingest_data.py)
MAX_NAME = 120
SYSTEM = "system"         # audit author of changes found by the folder re-check
TYPES = {".pdf": "PDF", ".docx": "Word", ".xlsx": "Excel", ".csv": "CSV", ".eml": "E-mail", ".txt": "Tekst",
         ".md": "Markdown"}
PREVIEW_CHARS = 300_000
_BAD_NAME = re.compile(r'[<>:"|?*\\/]')

_lock = threading.RLock()            # registry and trash
_cond = threading.Condition()        # the job queue
_queue: "OrderedDict[str, dict]" = OrderedDict()   # source -> {"kind": index|remove}; "*" -> folder re-check
_running: Optional[dict] = None
_errors: dict[str, str] = {}         # source -> why its last indexing failed (cleared by the next success)
_last_scan: Optional[dict] = None
_worker: Optional[threading.Thread] = None


class KnowledgeError(ValueError):
    """A request the panel refuses; the message is shown to the administrator (Polish)."""

    status = 400


class KnowledgeConflict(KnowledgeError):
    """A document of that name exists already (the panel then offers to replace it)."""

    status = 409


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------- places

def folder() -> Path:
    return get_settings().data_dir / "knowledge_base"


def registry_path() -> Path:
    return folder() / "registry.json"


def trash_dir() -> Path:
    return folder() / "trash"


def _trash_file() -> Path:
    return folder() / "trash.json"


def _private(path: Path) -> Path:
    """A folder only this account can read (company documents pass through it)."""
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def incoming_file() -> Path:
    """A new temporary file for an upload being received."""
    _private(folder())
    return _private(folder() / "incoming") / uuid.uuid4().hex


def _bases() -> tuple[Path, Path]:
    s = get_settings()
    return s.docs_dir, s.added_docs_dir


def _base_of(source: str) -> tuple[Path, str]:
    docs, added = _bases()
    if source.startswith(ADDED_PREFIX + "/"):
        return added, source[len(ADDED_PREFIX) + 1:]
    return docs, source


def _location(source: str) -> Path:
    """Where the document with this source path lives (whether it exists or not); never outside its folder."""
    base, rel = _base_of(str(source or ""))
    parts = Path(rel).parts
    if not rel or Path(rel).is_absolute() or ".." in parts or any(p.startswith(".") for p in parts):
        raise KnowledgeError("Nie ma takiego dokumentu w bazie wiedzy.")
    path = (base / rel).resolve()
    if base.resolve() not in path.parents or path.suffix.lower() not in SUPPORTED:
        raise KnowledgeError("Nie ma takiego dokumentu w bazie wiedzy.")
    return path


def _existing(source: str) -> Path:
    path = _location(source)
    if not path.is_file():
        raise KnowledgeError("Nie ma takiego dokumentu w bazie wiedzy (mógł zostać właśnie usunięty).")
    return path


def _writable(source: str) -> bool:
    """False for a folder inside the Hapilot checkout (outside its data folder): the demo documents."""
    base = _base_of(source)[0].resolve()
    root, data = PROJECT_ROOT.resolve(), get_settings().data_dir.resolve()
    inside = (lambda folder_, parent: folder_ == parent or parent in folder_.parents)
    return not (inside(base, root) and not inside(base, data))


def _check_writable(source: str) -> None:
    if not _writable(source):
        raise KnowledgeError("Folder dokumentów (HAPILOT_DOCS_DIR) to przykładowe dokumenty z repozytorium Hapilota — "
                             "ustaw w .env własny folder dokumentów, aby dodawać, zastępować i usuwać je w panelu.")


def _require_admin(user: Optional[str]) -> None:
    from src import admin

    if not admin.is_admin(user):
        raise KnowledgeError("Bazą wiedzy zarządza administrator.")


# --------------------------------------------------------------------------- small JSON stores

def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path: Path, data) -> None:
    _private(path.parent)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def registry() -> dict[str, dict]:
    """source -> who added the document and how (``panel``, ``czat``, ``folder``), who replaced or restored it."""
    return _read(registry_path(), {})


def _record(source: str, user: str, via: str) -> None:
    """A document was added (``via``: panel, czat or folder)."""
    with _lock:
        entries = registry()
        entries[source] = {"added_by": user, "added_at": _now(), "via": via}
        _write(registry_path(), entries)


def _note(source: str, **changes) -> None:
    """A later change of a document (replaced, restored); who added it stays."""
    with _lock:
        entries = registry()
        base = entries.get(source) or {"via": "czat" if source.startswith(ADDED_PREFIX + "/") else "folder"}
        entries[source] = {**base, **changes}
        _write(registry_path(), entries)


def record_added(sources: list[str], user: Optional[str], via: str = "czat") -> None:
    """Documents a user added from the chat (src/ingest_data.add_documents): who and when, in the audit log too."""
    from src import admin

    for source in sources:
        _record(source, user or "?", via)
        admin.audit(user or "?", "kb_added_chat", source, "dodany z czatu")


def trash() -> list[dict]:
    return _read(_trash_file(), [])


# --------------------------------------------------------------------------- checks

def check_name(raw) -> str:
    """The file name of an upload (the last part of a path a browser may send)."""
    name = unicodedata.normalize("NFC", str(raw or "")).replace("\\", "/").rsplit("/", 1)[-1].strip()
    if (not name or name.startswith(".") or len(name) > MAX_NAME or _BAD_NAME.search(name)
            or any(ord(c) < 32 for c in name)):
        raise KnowledgeError(f"Niepoprawna nazwa pliku: najwyżej {MAX_NAME} znaków, bez znaków < > : \" | ? * / \\ "
                             "i bez kropki na początku.")
    if Path(name).suffix.lower() not in SUPPORTED:
        raise KnowledgeError(f"Nieobsługiwany typ pliku {name!r} — dozwolone: {', '.join(SUPPORTED)}.")
    return name


def check_content(name: str, path: Path) -> None:
    """The content matches the type the name promises (a quick look; the indexing reads it fully)."""
    size = path.stat().st_size
    if size == 0:
        raise KnowledgeError(f"Plik {name} jest pusty.")
    with path.open("rb") as fh:
        head = fh.read(8192)
    suffix = Path(name).suffix.lower()
    ok = {".pdf": b"%PDF-" in head[:1024], ".docx": head.startswith(b"PK\x03\x04"),
          ".xlsx": head.startswith(b"PK\x03\x04")}.get(suffix, b"\x00" not in head)
    if not ok:
        raise KnowledgeError(f"Zawartość pliku {name} nie pasuje do typu {TYPES[suffix]} ({suffix}).")


def _same_content(digest: str, size: int, skip: Optional[str] = None) -> Optional[str]:
    """Source path of a document with exactly this content (files of another size are not even read)."""
    from src import ingest_data

    docs, added = _bases()
    for path, rel in ingest_data.corpus_entries(docs, added):
        if rel != skip and path.stat().st_size == size and _sha256(path) == digest:
            return rel
    return None


# --------------------------------------------------------------------------- changes

def _place(new_file: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(new_file), str(target))
    target.chmod(0o600)


def _to_trash(source: str, path: Path, user: str, reason: str) -> dict:
    """Move a document's file into the trash (``reason``: deleted or replaced)."""
    entry_id = f"{datetime.now():%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}"
    dest = _private(_private(trash_dir()) / entry_id) / path.name
    entry = {"id": entry_id, "source": source, "name": path.name, "reason": reason, "by": user, "at": _now(),
             "size": path.stat().st_size, "sha256": _sha256(path), "registry": registry().get(source)}
    shutil.move(str(path), str(dest))
    _write(_trash_file(), trash() + [entry])
    return entry


def upload(name: str, data_file: Path, user: str, replace: bool = False) -> dict:
    """A new document from the panel, saved into the company documents folder and queued for indexing.
    With ``replace``, an existing document of that name becomes the new version (the old one goes to the trash)."""
    from src import admin

    try:
        _require_admin(user)
        name = check_name(name)
        if (_location(name)).is_file():
            if not replace:
                raise KnowledgeConflict(f"Dokument {name} jest już w bazie wiedzy. Zastąpić go nową wersją?")
            return replace_document(name, data_file, user)
        _check_writable(name)
        check_content(name, data_file)
        size = data_file.stat().st_size
        with _lock:
            duplicate = _same_content(_sha256(data_file), size)
            if duplicate:
                raise KnowledgeError(f"Ten sam dokument jest już w bazie wiedzy: {duplicate}.")
            _place(data_file, _location(name))
            _record(name, user, "panel")
        admin.audit(user, "kb_uploaded", name, _size(size))
        submit_index(name)
        return {"source": name, "replaced": False}
    finally:
        data_file.unlink(missing_ok=True)


def replace_document(source: str, data_file: Path, user: str) -> dict:
    """A new version of a document under the same name; the previous one goes to the trash."""
    from src import admin

    try:
        _require_admin(user)
        current = _existing(source)
        _check_writable(source)
        check_content(current.name, data_file)
        digest, size = _sha256(data_file), data_file.stat().st_size
        with _lock:
            if digest == _sha256(current):
                raise KnowledgeError("Nowa wersja jest taka sama jak obecna — nic nie zmieniono.")
            duplicate = _same_content(digest, size, skip=source)
            if duplicate:
                raise KnowledgeError(f"Ten sam dokument jest już w bazie wiedzy: {duplicate}.")
            _to_trash(source, current, user, "replaced")
            _place(data_file, current)
            _note(source, replaced_by=user, replaced_at=_now())
        admin.audit(user, "kb_replaced", source, _size(size))
        submit_index(source)
        return {"source": source, "replaced": True}
    finally:
        data_file.unlink(missing_ok=True)


def delete(source: str, user: str) -> dict:
    """Move a document to the trash and take it out of the index."""
    from src import admin

    _require_admin(user)
    path = _existing(source)
    _check_writable(source)
    with _lock:
        entry = _to_trash(source, path, user, "deleted")
        entries = registry()
        if entries.pop(source, None) is not None:
            _write(registry_path(), entries)
    admin.audit(user, "kb_deleted", source, f"w koszu przez {get_settings().kb_trash_days} dni")
    submit_remove(source)
    return {"deleted": source, "trash_id": entry["id"]}


def restore(entry_id: str, user: str) -> dict:
    """Put a document from the trash back. A newer version with that name goes to the trash in its place."""
    from src import admin

    _require_admin(user)
    with _lock:
        entries = trash()
        entry = next((e for e in entries if e.get("id") == entry_id), None)
        stored = trash_dir() / str(entry_id) / str((entry or {}).get("name", ""))
        if entry is None or not re.fullmatch(r"[0-9]{14}-[0-9a-f]{8}", str(entry_id)) or not stored.is_file():
            raise KnowledgeError("Nie ma takiej pozycji w koszu (mogła zostać usunięta po upływie czasu przechowywania).")
        source = entry["source"]
        target = _location(source)
        _check_writable(source)
        duplicate = _same_content(entry["sha256"], entry["size"], skip=source)
        if duplicate:
            raise KnowledgeError(f"Ten sam dokument jest już w bazie wiedzy: {duplicate}.")
        replaced = target.is_file()
        if replaced:
            if _sha256(target) == entry["sha256"]:
                raise KnowledgeError("Ta wersja dokumentu jest już w bazie wiedzy.")
            _to_trash(source, target, user, "replaced")
            entries = trash()
        _place(stored, target)
        shutil.rmtree(stored.parent, ignore_errors=True)
        _write(_trash_file(), [e for e in entries if e.get("id") != entry_id])
        if entry.get("registry") and not replaced:
            current = registry()
            current[source] = entry["registry"]
            _write(registry_path(), current)
        _note(source, restored_by=user, restored_at=_now())
    admin.audit(user, "kb_restored", source, "w miejsce nowszej wersji (ta trafiła do kosza)" if replaced else "")
    submit_index(source)
    return {"restored": source, "replaced": replaced}


def purge_trash(now: Optional[datetime] = None) -> int:
    """Delete trash entries older than ``HAPILOT_KB_TRASH_DAYS`` for good. Returns how many."""
    limit = (now or datetime.now(timezone.utc)) - timedelta(days=get_settings().kb_trash_days)
    with _lock:
        entries = trash()
        keep, old = [], []
        for entry in entries:
            (old if _parse(entry.get("at")) < limit else keep).append(entry)
        for entry in old:
            shutil.rmtree(trash_dir() / str(entry.get("id")), ignore_errors=True)
        if old:
            _write(_trash_file(), keep)
    return len(old)


def _parse(stamp: Optional[str]) -> datetime:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(1, round(n / 1024))} kB"


# --------------------------------------------------------------------------- indexing in the background

def submit_index(source: str) -> None:
    _submit(source, "index")


def submit_remove(source: str) -> None:
    _submit(source, "remove")


def request_rescan() -> None:
    _submit("*", "rescan")


def _submit(key: str, kind: str) -> None:
    with _cond:
        _queue.pop(key, None)  # the latest request for a document wins (e.g. delete, then restore)
        _queue[key] = {"kind": kind, "since": _now()}
        _cond.notify_all()


def run_pending() -> int:
    """Process the queued jobs in this thread, oldest first (the worker thread; tests). Returns how many."""
    global _running
    done = 0
    while True:
        with _cond:
            if not _queue:
                return done
            key, job = _queue.popitem(last=False)
            _running = {"source": key, **job, "started": _now()}
        try:
            _process(key, job)
        except Exception as exc:  # the worker keeps running; the panel shows the error on the document
            if key != "*":
                _errors[key] = f"{type(exc).__name__}: {exc}"
            print(f"⚠️ [Baza wiedzy] {key}: {type(exc).__name__}: {exc}", flush=True)
        finally:
            with _cond:
                _running = None
                _cond.notify_all()
        done += 1


def _process(key: str, job: dict) -> None:
    from src import ingest_data, ui_activity

    if job["kind"] == "rescan":
        rescan()
        return
    s = get_settings()
    docs, added = _bases()
    with ui_activity.busy():  # the updater does not restart the UI meanwhile
        path = _location(key)
        if job["kind"] == "index" and path.is_file():
            facts = ingest_data.index_entries([(path, key)], collection=s.collection, docs_dir=docs, added_dir=added,
                                              path=s.chroma_path)[key]
            print(f"📚 [Baza wiedzy] zaindeksowano: {key} ({facts.get('chunks', 0)} fragm."
                  + (f"; błąd: {facts['error']}" if facts.get("error") else "") + ")", flush=True)
        else:
            ingest_data.remove_sources([key], collection=s.collection, docs_dir=docs, added_dir=added, path=s.chroma_path)
            print(f"📚 [Baza wiedzy] usunięto z indeksu: {key}", flush=True)
    _errors.pop(key, None)


def rescan() -> dict:
    """Compare the folders with the index: index new and changed documents, remove the ones deleted from the folder.
    Documents waiting in the queue are left to their own job. Also empties the trash of old entries."""
    global _last_scan
    from src import admin, ingest_data, knowledge_base, ui_activity

    s = get_settings()
    docs, added = _bases()
    with _cond:
        waiting = set(_queue)
    with knowledge_base.index_lock:  # a chat upload is either complete or not started
        listed = ingest_data.current_files(s.collection, s.chroma_path, docs, added)
        entries = [(p, rel) for p, rel in ingest_data.corpus_entries(docs, added) if rel not in waiting]
        new, changed = [], []
        for path, rel in entries:
            known = listed.get(rel)
            stat = path.stat()
            if known is None:
                new.append((path, rel))
            elif (known.get("size"), known.get("mtime")) != (stat.st_size, stat.st_mtime) and \
                    _sha256(path) != known.get("sha256"):
                changed.append((path, rel))
        on_disk = {rel for _p, rel in ingest_data.corpus_entries(docs, added)}
        gone = sorted(rel for rel in listed if rel not in on_disk and rel not in waiting)
    if new or changed or gone:
        with ui_activity.busy():
            if gone:
                ingest_data.remove_sources(gone, collection=s.collection, docs_dir=docs, added_dir=added,
                                           path=s.chroma_path)
            if new or changed:
                ingest_data.index_entries(new + changed, collection=s.collection, docs_dir=docs, added_dir=added,
                                          path=s.chroma_path)
        known_sources = registry()
        for _path, rel in new:
            if rel not in known_sources:
                _record(rel, SYSTEM, "folder")
        for action, items in (("kb_folder_added", [r for _p, r in new]), ("kb_folder_changed", [r for _p, r in changed]),
                              ("kb_folder_removed", gone)):
            for rel in items:
                admin.audit(SYSTEM, action, rel, "zmiana w folderze na serwerze")
        print(f"📚 [Baza wiedzy] folder: nowe {len(new)}, zmienione {len(changed)}, usunięte {len(gone)}", flush=True)
    purged = purge_trash()
    _last_scan = {"at": _now(), "added": len(new), "changed": len(changed), "removed": len(gone), "purged": purged}
    return _last_scan


def _loop(interval: float) -> None:
    next_scan = time.monotonic() + interval
    while True:
        run_pending()
        with _cond:
            if not _queue:
                _cond.wait(max(1.0, next_scan - time.monotonic()) if interval else None)
        if interval and time.monotonic() >= next_scan:
            request_rescan()
            next_scan = time.monotonic() + interval


def start() -> None:
    """The UI process (src/app.py): the background thread, a folder re-check at once and every few minutes."""
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    request_rescan()
    _worker = threading.Thread(target=_loop, args=(get_settings().kb_rescan_minutes * 60,), name="hapilot-knowledge",
                               daemon=True)
    _worker.start()


def wait_idle(timeout: float = 30) -> bool:
    """True once nothing is queued or running (tests and the panel's "check now")."""
    deadline = time.monotonic() + timeout
    with _cond:
        while _queue or _running:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            _cond.wait(left)
    return True


# --------------------------------------------------------------------------- what the panel shows

def overview(user: str) -> dict:
    """Every document with its facts and status, the trash, the queue and the limits."""
    from src import ingest_data

    _require_admin(user)
    s = get_settings()
    docs, added = _bases()
    listed = ingest_data.read_files(s.collection, s.chroma_path) or {}
    who = registry()
    with _cond:
        queued, running = dict(_queue), dict(_running) if _running else None
    files, total_size = [], 0
    for path, rel in ingest_data.corpus_entries(docs, added):
        stat = path.stat()
        total_size += stat.st_size
        facts = listed.get(rel) or {}
        if running and running["source"] == rel:
            status = "indexing"
        elif rel in queued:
            status = "queued"
        elif rel in _errors:
            status, facts = "error", {**facts, "error": _errors[rel]}
        elif not facts or (facts.get("size"), facts.get("mtime")) != (stat.st_size, stat.st_mtime):
            status = "pending"  # not indexed yet (copied into the folder): the next re-check takes it
        else:
            status = "error" if facts.get("error") else "indexed"
        origin = "dodane" if rel.startswith(ADDED_PREFIX + "/") else "firma"
        entry = who.get(rel) or {"via": "czat" if origin == "dodane" else "folder"}
        files.append({
            "source": rel, "name": path.name, "folder": str(Path(rel).parent) if "/" in rel else "", "origin": origin,
            "type": TYPES.get(path.suffix.lower(), path.suffix), "ext": path.suffix.lower(), "size": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "pages": facts.get("pages"), "sheets": facts.get("sheets"), "chunks": facts.get("chunks"),
            "ocr_pages": facts.get("ocr_pages"), "ocr_confidence": facts.get("ocr_confidence"),
            "indexed_at": facts.get("indexed_at"), "error": facts.get("error"), "status": status,
            "added_by": entry.get("added_by"), "added_at": entry.get("added_at"), "via": entry.get("via"),
            "replaced_by": entry.get("replaced_by"), "replaced_at": entry.get("replaced_at"),
            "writable": _writable(rel)})
    on_disk = {f["source"] for f in files}
    for rel, facts in sorted(listed.items()):  # deleted or moved away, still searchable until its removal job runs
        if rel not in on_disk:
            files.append({
                "source": rel, "name": Path(rel).name, "folder": str(Path(rel).parent) if "/" in rel else "",
                "origin": "dodane" if rel.startswith(ADDED_PREFIX + "/") else "firma",
                "type": TYPES.get(Path(rel).suffix.lower(), Path(rel).suffix), "ext": Path(rel).suffix.lower(),
                "size": facts.get("size") or 0, "modified": None, "pages": facts.get("pages"), "sheets": facts.get("sheets"),
                "chunks": facts.get("chunks"), "ocr_pages": facts.get("ocr_pages"), "ocr_confidence": facts.get("ocr_confidence"),
                "indexed_at": facts.get("indexed_at"), "error": None, "status": "removing", "added_by": None,
                "added_at": None, "via": (who.get(rel) or {}).get("via"), "replaced_by": None, "replaced_at": None,
                "writable": False})
    days = s.kb_trash_days
    bin_ = [{**{k: e.get(k) for k in ("id", "source", "name", "reason", "by", "at", "size")},
             "days_left": max(0, days - (datetime.now(timezone.utc) - _parse(e.get("at"))).days)}
            for e in reversed(trash())]
    return {
        "files": files, "trash": bin_,
        "totals": {"files": len(on_disk), "size": total_size, "chunks": sum(int(f["chunks"] or 0) for f in files),
                   "ocr_pages": sum(int(f["ocr_pages"] or 0) for f in files)},
        "queue": {"waiting": len([k for k in queued if k != "*"]), "running": running["source"] if running else None,
                  "rescan": "*" in queued or bool(running and running["source"] == "*")},
        "last_scan": _last_scan, "writable": _writable(""),
        "folders": {"docs": str(docs), "added": str(added)},
        "limits": {"max_bytes": s.kb_max_upload_mb * 1024 * 1024, "max_mb": s.kb_max_upload_mb, "types": list(SUPPORTED),
                   "rescan_minutes": s.kb_rescan_minutes, "trash_days": days}}


def text(source: str, user: str) -> dict:
    """The text the index got from a document, per page or sheet (pages read by OCR marked, with confidence)."""
    from src.extractors import ExtractionError, extract

    _require_admin(user)
    path = _existing(source)
    try:
        segments = extract(path)
    except ExtractionError as exc:
        return {"source": source, "name": path.name, "error": str(exc), "segments": [], "truncated": False}
    out, used, truncated = [], 0, False
    for seg in segments:
        if used >= PREVIEW_CHARS:
            truncated = True
            break
        body = seg.text[:PREVIEW_CHARS - used]
        truncated = truncated or len(body) < len(seg.text)
        used += len(body)
        label = (f"Strona {seg.meta['page']}" if "page" in seg.meta
                 else f"Arkusz {seg.meta['sheet']}" if "sheet" in seg.meta else "")
        out.append({"label": label, "ocr": bool(seg.meta.get("ocr")), "confidence": seg.meta.get("ocr_confidence"),
                    "text": body})
    return {"source": source, "name": path.name, "error": None, "segments": out, "truncated": truncated}


def file_for(source: str, user: str) -> Path:
    """The original file of a document (download / open in the browser)."""
    _require_admin(user)
    return _existing(source)
