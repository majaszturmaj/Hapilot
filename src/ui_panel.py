"""The UI's right-hand panel ("Źródła i pliki"): Chainlit glue for src/source_view.py.

Per request the UI keeps a ``PendingTurn`` (workspace snapshot + retrieval observer). It survives a
human-in-the-loop pause, so a request that is confirmed and continued is still one turn. When the
answer is ready, ``finish_turn`` builds the panel data; the panel opens only if the turn has
sources or changed files.

PDF page images and downloads are served by one route, ``/hapilot/panel/{token}``. A token is a
random capability bound to one chat and one file (and page); it is handed only to that chat's
browser and forgotten when the chat ends (a resumed chat restores its tokens from the chat history,
src/ui_history.py). The route never takes a path from the request.
"""

from __future__ import annotations

import mimetypes
import secrets
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

from src import run_context, source_view
from src.config import get_settings

ROUTE_PREFIX = "/hapilot/panel"
MAX_TURNS = 10            # turns kept in the panel per conversation
SIDEBAR_TITLE = "📎 Źródła i pliki"

_tokens: dict[str, dict] = {}       # token -> {"session", "path", "page"}
_tokens_lock = threading.Lock()


# --------------------------------------------------------------------------- tokens and the route

def _register(session: str, path: Path, page: Optional[int] = None) -> str:
    with _tokens_lock:
        for token, entry in _tokens.items():  # one token per (session, file, page)
            if entry["session"] == session and entry["path"] == path and entry["page"] == page:
                return token
        token = secrets.token_urlsafe(24)
        _tokens[token] = {"session": session, "path": path, "page": page}
        return token


def url_for(session: str, path: Path, page: Optional[int] = None) -> str:
    return _url(_register(session, path, page))


def export_tokens(session: str) -> dict:
    """The session's tokens, to store with a chat (``{token: [path, page]}``)."""
    with _tokens_lock:
        return {t: [str(e["path"]), e["page"]] for t, e in _tokens.items() if e["session"] == session}


def import_tokens(session: str, tokens: dict) -> None:
    """Restore stored tokens of a resumed chat, so its panel URLs work again."""
    with _tokens_lock:
        for token, (path, page) in (tokens or {}).items():
            _tokens[token] = {"session": session, "path": Path(path), "page": page}


def forget_session(session: str) -> None:
    with _tokens_lock:
        for token in [t for t, e in _tokens.items() if e["session"] == session]:
            del _tokens[token]


def _url(token: str) -> str:
    try:
        from chainlit.config import config

        root = (config.run.root_path or "").rstrip("/")
    except Exception:
        root = ""
    return f"{root}{ROUTE_PREFIX}/{token}"


@lru_cache(maxsize=64)
def _page_png(path: str, mtime: float, page: int) -> bytes:  # mtime: a changed file is rendered again
    return source_view.render_pdf_page(Path(path), page)


def serve(token: str):
    """Response for a panel URL: a PDF page as PNG, or a file download."""
    from starlette.responses import FileResponse, Response

    entry = _tokens.get(token)
    path = entry["path"] if entry else None
    if path is None or not path.is_file():
        return Response(status_code=404)
    headers = {"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"}
    if entry["page"]:
        return Response(_page_png(str(path), path.stat().st_mtime, entry["page"]), media_type="image/png", headers=headers)
    media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media, filename=path.name, headers=headers)


_installed = False


def install_route() -> None:
    """Add the panel route to Chainlit's server, ahead of its catch-all page route."""
    global _installed
    if _installed:
        return
    from chainlit.server import app

    app.add_api_route(ROUTE_PREFIX + "/{token}", serve, methods=["GET"], include_in_schema=False)
    app.router.routes.insert(0, app.router.routes.pop())

    @app.middleware("http")
    async def revalidate_public(request, call_next):
        """Browsers must re-check /public (custom elements, panel_tab.js, CSS): without this an updated form
        component kept running from the browser cache after a deployment."""
        response = await call_next(request)
        if request.url.path.startswith("/public/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    _installed = True


# --------------------------------------------------------------------------- turns

@dataclass
class PendingTurn:
    question: str
    before: dict
    observer: source_view.RunObserver = field(default_factory=source_view.RunObserver)


def start_turn(session: str, question: str) -> PendingTurn:
    with run_context.use_context(session_id=session):
        workspace = run_context.workspace_dir()
    return PendingTurn(question=question, before=source_view.snapshot(workspace))


def build_turn(session: str, pending: PendingTurn, answer: str) -> Optional[dict]:
    """Panel data for a finished request, or None when it used no documents and changed no files."""
    settings = get_settings()
    with run_context.use_context(session_id=session):
        workspace = run_context.workspace_dir()
    docs = source_view.source_docs(pending.observer, answer, settings.docs_dir, settings.added_docs_dir, workspace)
    changed = source_view.changes(pending.before, source_view.snapshot(workspace))
    if not docs and not changed:
        return None

    def page_url(path: Path, n: int) -> str:
        return _url(_register(session, path, n))

    files = [_file_item(session, workspace / c.name, c.name, c.status, c.size, c.preview, c.diff) for c in changed]
    return {"question": _question(pending.question), "sources": [d.describe(page_url) for d in docs], "files": files}


def _question(text: str) -> str:
    question = " ".join((text or "").split())
    return question[:120] + ("…" if len(question) > 120 else "")


def _file_item(session: str, path: Path, name: str, status: str, size: int, preview: Optional[str] = None,
               diff: Optional[str] = None) -> dict:
    """A file of the panel's "Pliki" list; a PDF gets its pages as images."""
    item = {"name": name, "status": status, "size": size, "url": _url(_register(session, path)), "preview": preview,
            "diff": diff}
    if path.suffix.lower() == ".pdf":
        try:
            item["pages"] = [{"n": n, "url": _url(_register(session, path, n)), "ratio": round(h / w, 4)}
                             for n, (w, h) in enumerate(source_view.pdf_page_sizes(path), 1)]
        except Exception as exc:
            print(f"⚠️ [PANEL] {name}: {exc}")
    return item


# --------------------------------------------------------------------------- engine v3

MAX_AGENT_SOURCES = 15  # documents shown for one v3 answer


def agent_source(container_path: str, mirror: dict, uploads: Optional[Path]) -> Optional[str]:
    """The index source (or ``załącznik:<name>``) behind a path the v3 agent opened in its container: an original
    in /praca/dokumenty or /praca/dodane, a text copy in /praca/tekst (``mirror``: mirror file name → source), or an
    upload or its text copy in /praca/zalaczniki. None for anything else (results, folders, unknown files)."""
    from src.agent_workspace import ADDED, ADDED_PREFIX, DOCS, ROOT, TEXT, UPLOADS

    def rest(folder: str) -> Optional[str]:
        prefix = f"{ROOT}/{folder}/"
        return container_path[len(prefix):] if container_path.startswith(prefix) else None

    if (name := rest(DOCS)) is not None:
        return name or None
    if (name := rest(ADDED)) is not None:
        return f"{ADDED_PREFIX}/{name}" if name else None
    if (name := rest(TEXT)) is not None:
        return mirror.get(name)
    if (name := rest(UPLOADS)) is not None and uploads is not None and "/" not in name:
        candidate = name  # a text copy is <upload>.txt or <upload>.<sheet>.csv: strip suffixes to the upload
        while Path(candidate).suffix:
            candidate = candidate[: -len(Path(candidate).suffix)]
            if candidate and (uploads / candidate).is_file():
                return source_view.ATTACHMENT_PREFIX + candidate
        return source_view.ATTACHMENT_PREFIX + name if (uploads / name).is_file() else None
    return None


def agent_sources(retrievals: list, opened: list, answer: str, uploads: Optional[Path]) -> list:
    """Documents behind a v3 answer: those named in the answer, those the agent read, those its searches returned
    (with the fragments highlighted); named ones first, then read ones."""
    from src import text_mirror

    s = get_settings()
    manifest = text_mirror.read_manifest(s.chroma_path, s.collection).get("files") or {}
    mirror = {out: source for source, entry in manifest.items() for out in entry.get("outputs") or []}
    chunks, seen = [], set()
    for retrieval in retrievals or []:
        for chunk in retrieval.get("chunks") or []:
            key = chunk.get("chunk_id") or (chunk.get("file"), chunk.get("page"), chunk.get("text"))
            if key not in seen:
                seen.add(key)
                chunks.append(dict(chunk, collection=s.collection))
    groups = source_view.group_chunks(chunks)
    read = list(dict.fromkeys(filter(None, (agent_source(p, mirror, uploads) for p in opened or []))))
    named = [source for source in manifest if source_view.is_cited(source, answer)]
    docs = []
    for source in dict.fromkeys(named + read + list(groups)):
        path = source_view.resolve_source(source, Path(s.docs_dir), Path(s.added_docs_dir), uploads)
        if path is None and source not in groups:
            continue  # a path the agent tried that is no document: nothing to show
        docs.append(source_view.SourceDoc(source, path, groups.get(source, []), source_view.is_cited(source, answer),
                                          read_by_agent=source in read))
    docs.sort(key=lambda d: (not d.cited, not d.read_by_agent))  # stable
    return docs[:MAX_AGENT_SOURCES]


def build_agent_turn(session: str, question: str, answer: str, files: list, retrievals: list, opened: list,
                     uploads: Optional[Path]) -> Optional[dict]:
    """Panel data for a v3 answer (src/ui_agent.py), or None when it has no documents and no files.
    ``files``: the engine's new or changed files in /praca/wyniki (``{"name", "path", "bytes", "status"}``)."""
    docs = agent_sources(retrievals, opened, answer, uploads)
    pdf_stems = {Path(f["name"]).with_suffix("").as_posix() for f in files if f["name"].lower().endswith(".pdf")}
    items = []
    for f in files:
        name, path = f["name"], Path(f["path"])
        if source_view._hidden(Path(name).name) or (name.lower().endswith(".tex")
                                                     and Path(name).with_suffix("").as_posix() in pdf_stems):
            continue
        preview = None
        if path.suffix.lower() in source_view.TEXT_PREVIEW and f.get("bytes", 0) <= source_view.PREVIEW_LIMIT:
            preview = path.read_bytes().decode("utf-8", errors="replace")
        items.append(_file_item(session, path, name, f.get("status") or "created", f.get("bytes", 0), preview))
    if not docs and not items:
        return None
    return {"question": _question(question), "sources": [d.describe(lambda p, n: _url(_register(session, p, n)))
                                                          for d in docs], "files": items}


IMAGE_TYPES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
ATTACH_LIMIT = 10  # files attached under one answer


def answer_files(session: str, pending: Optional[PendingTurn], text: str, uploads: list[str]) -> list[Path]:
    """Files to attach under an answer, whatever happens to the side panel: those the request created or changed
    (e.g. a chart saved by code), then earlier files of this chat that the answer names (a request to send one again).
    Never the user's own uploads (``uploads``), never hidden helper files (scripts, .aux, signatures), and not the .tex
    written next to a generated PDF."""
    with run_context.use_context(session_id=session):
        workspace = run_context.workspace_dir()
    if not workspace.is_dir():
        return []
    files = [workspace / c.name for c in source_view.changes(pending.before, source_view.snapshot(workspace))] \
        if pending is not None else []
    pdf_stems = {p.stem for p in files if p.suffix.lower() == ".pdf"}
    files = [p for p in files if not (p.suffix.lower() == ".tex" and p.stem in pdf_stems)]
    for path in sorted(workspace.iterdir()):
        if (path.is_file() and len(path.name) >= 5 and path.name in (text or "") and path not in files
                and not source_view._hidden(path.name)):
            files.append(path)
    return [p for p in files if p.name not in uploads and p.is_file()][:ATTACH_LIMIT]


def add_turn(turns: list, turn: dict) -> list:
    """Append a turn with the next id (ids stay stable when old turns are dropped)."""
    turn["id"] = turns[-1]["id"] + 1 if turns else 1
    return (turns + [turn])[-MAX_TURNS:]


async def show(turns: list, active: int) -> None:
    """Open the right-hand panel on the turn with id ``active``."""
    import chainlit as cl

    element = cl.CustomElement(name="workspace_panel", display="side", props={"turns": turns, "active": active})
    await cl.ElementSidebar.set_title(SIDEBAR_TITLE)
    await cl.ElementSidebar.set_elements([element], key=f"panel-{len(turns)}-{active}")
