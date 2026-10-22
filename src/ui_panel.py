"""The UI's right-hand panel ("Źródła i pliki"): Chainlit glue for src/source_view.py.

Per request the UI keeps a ``PendingTurn`` (workspace snapshot + retrieval observer). It survives a
human-in-the-loop pause, so a request that is confirmed and continued is still one turn. When the
answer is ready, ``finish_turn`` builds the panel data; the panel opens only if the turn has
sources or changed files.

PDF page images and downloads are served by one route, ``/hapilot/panel/{token}``. A token is a
random capability bound to one chat session and one file (and page); it is handed only to that
session's browser and forgotten when the chat ends. The route never takes a path from the request.
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

    files = []
    for change in changed:
        path = workspace / change.name
        item = {"name": change.name, "status": change.status, "size": change.size,
                "url": _url(_register(session, path)), "preview": change.preview, "diff": change.diff}
        if path.suffix.lower() == ".pdf":
            try:
                item["pages"] = [{"n": n, "url": page_url(path, n), "ratio": round(h / w, 4)}
                                 for n, (w, h) in enumerate(source_view.pdf_page_sizes(path), 1)]
            except Exception as exc:
                print(f"⚠️ [PANEL] {change.name}: {exc}")
        files.append(item)
    question = " ".join(pending.question.split())
    return {"question": question[:120] + ("…" if len(question) > 120 else ""),
            "sources": [d.describe(page_url) for d in docs], "files": files}


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
