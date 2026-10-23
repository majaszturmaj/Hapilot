"""Is the UI busy? A small status file read by the updater (``scripts/hapilotctl.py update``).

While a chat handler runs — an answer, a document form the user is filling in, a document being compiled again —
the UI counts it in ``<data dir>/run/ui_activity.json``, together with its PID and the time of the last change.
The updater restarts the UI only when nothing runs and nothing has happened for a while, so an update never cuts
an answer short.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

from src.config import get_settings

_lock = threading.Lock()
_active = 0


def status_file() -> Path:
    return get_settings().data_dir / "run" / "ui_activity.json"


def _change(delta: int) -> None:
    global _active
    with _lock:
        _active = max(0, _active + delta)
        path = status_file()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps({"pid": os.getpid(), "active": _active, "updated": time.time()}))
            os.replace(tmp, path)
        except OSError:
            pass  # the status file is a courtesy for the updater; the chat never fails because of it


def tracked(func):
    """Decorator for async Chainlit handlers: counted as running while they run."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        _change(+1)
        try:
            return await func(*args, **kwargs)
        finally:
            _change(-1)

    return wrapper


@contextlib.contextmanager
def busy():
    """Counted as running for the duration (background work of the UI, e.g. indexing a document)."""
    _change(+1)
    try:
        yield
    finally:
        _change(-1)


def idle(ui_pid: Optional[int], idle_seconds: float, path: Optional[Path] = None,
         now: Optional[float] = None) -> tuple[bool, str]:
    """(may the UI process ``ui_pid`` be restarted now, why)."""
    path = path or status_file()
    try:
        info = json.loads(path.read_text())
    except (OSError, ValueError):
        return True, "no UI activity recorded"
    if ui_pid is None or info.get("pid") != ui_pid:
        return True, "no activity of the running UI recorded"
    if int(info.get("active", 0)) > 0:
        return False, f"{info['active']} chat action(s) in progress"
    age = (now if now is not None else time.time()) - float(info.get("updated", 0))
    if age < idle_seconds:
        return False, f"last activity {age / 60:.0f} min ago (waiting for {idle_seconds / 60:.0f} min without activity)"
    return True, f"idle for {age / 60:.0f} min"
