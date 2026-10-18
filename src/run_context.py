"""Per-run context: session, model profile, approval policy and "today" for the current request.

The values travel in LangGraph's ``RunnableConfig["configurable"]`` (built by ``make_config``), so
nodes and tools can read them without extra parameters. Outside a graph run (scripts, MCP servers,
tests) ``use_context(...)`` provides them; otherwise the defaults from Settings apply.

Each session gets its own workspace directory; generated files never leave it.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
from pathlib import Path
from typing import Any, Iterator, Optional

from src.config import get_settings

_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")
_LOCAL: ContextVar[dict] = ContextVar("hapilot_run_context", default={})


def _configurable() -> dict:
    try:
        from langgraph.config import get_config

        return dict(get_config().get("configurable") or {})
    except Exception:  # not inside a graph run
        return {}


def current(key: str, default: Any = None) -> Any:
    value = _configurable().get(key)
    if value is None:
        value = _LOCAL.get().get(key)
    return default if value is None else value


@contextmanager
def use_context(**values: Any) -> Iterator[None]:
    """Set context values for code running outside a graph (and for tests)."""
    token = _LOCAL.set({**_LOCAL.get(), **values})
    try:
        yield
    finally:
        _LOCAL.reset(token)


def session_id() -> str:
    sid = _SAFE_ID.sub("_", str(current("session_id", "default")))[:64]
    return sid.strip(".") or "default"


def profile_name() -> str:
    return current("profile") or get_settings().default_profile


def approval_policy() -> str:
    return current("approval_policy") or get_settings().approval_policy


def today() -> date:
    value = current("today")
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        return date.fromisoformat(value)
    return date.today()


def workspace_dir() -> Path:
    """The current session's workspace (created on demand). Evaluation runs set ``workspace_root``."""
    root = current("workspace_root")
    path = (Path(root) if root else get_settings().workspace_root) / session_id()
    path.mkdir(parents=True, exist_ok=True)
    return path


def workspace_file(name: str) -> Path:
    """Resolve a file name inside the session workspace; refuses paths that escape it."""
    base = workspace_dir().resolve()
    candidate = (base / name).resolve()
    if candidate.parent != base:
        raise ValueError(f"File name {name!r} is not a plain name inside the session workspace")
    return candidate


def make_config(
    session_id: str,
    *,
    profile: Optional[str] = None,
    approval_policy: Optional[str] = None,
    today: Optional[str] = None,
    recursion_limit: Optional[int] = None,
    callbacks: Optional[list] = None,
    **extra: Any,
) -> dict:
    """RunnableConfig for one graph invocation."""
    settings = get_settings()
    configurable = {"session_id": session_id, "profile": profile or settings.default_profile,
                    "approval_policy": approval_policy or settings.approval_policy}
    if today:
        configurable["today"] = today
    configurable.update({k: v for k, v in extra.items() if v is not None})
    config: dict = {"recursion_limit": recursion_limit or settings.recursion_limit, "configurable": configurable}
    if callbacks:
        config["callbacks"] = callbacks
    return config
