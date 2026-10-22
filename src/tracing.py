"""Execution traces for evaluation runs (``hapilot.trace/1``, see docs/EVALUATION_INTERFACE.md).

A ``TraceRecorder`` is attached to a run through the run context (``configurable["trace"]``). Code
anywhere in Hapilot calls ``tracing.event(...)``; without a recorder (the UI) nothing is written. A run may
also carry an ``observer`` callable (``configurable["observer"]``) that receives every event; the UI
uses it for its side panel (src/source_view.py).
Large payloads (prompts, responses, long tool arguments) are stored once in ``blobs/<sha256>.json``
and referenced as ``"sha256:<hex>"``.

The run context may also carry a ``deadline`` (time.monotonic); ``check_deadline`` raises
``TaskTimeout`` once it has passed. Nodes and model calls check it.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import threading
import time
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

SCHEMA = "hapilot.trace/1"
_INLINE_LIMIT = 500
_DEPTH: ContextVar[int] = ContextVar("hapilot_tool_depth", default=0)


class TaskTimeout(RuntimeError):
    """The task's time budget is exhausted."""


class TraceRecorder:
    def __init__(self, path: Path, blobs_dir: Path, run_id: str, task_id: str):
        self.path, self.blobs_dir, self.run_id, self.task_id = path, blobs_dir, run_id, task_id
        path.parent.mkdir(parents=True, exist_ok=True)
        blobs_dir.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()
        self.seq = 0
        self.llm_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.sources: list[str] = []      # files whose text entered a model prompt, in order of first use
        self.plan_tools: list[str] = []

    def emit(self, type_: str, **fields: Any) -> None:
        with self._lock:
            self.seq += 1
            line = {"schema": SCHEMA, "run_id": self.run_id, "task_id": self.task_id, "seq": self.seq,
                    "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"), "type": type_, **fields}
            self._fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
            self._fh.flush()

    def blob(self, obj: Any) -> str:
        data = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(data.encode("utf-8")).hexdigest()
        path = self.blobs_dir / f"{digest}.json"
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            tmp.replace(path)
        return f"sha256:{digest}"

    def close(self) -> None:
        with self._lock:
            self._fh.close()


def current() -> Optional[TraceRecorder]:
    from src import run_context

    rec = run_context.current("trace")
    return rec if isinstance(rec, TraceRecorder) else None


def event(type_: str, **fields: Any) -> None:
    _notify_observer(type_, fields)
    rec = current()
    if rec is None:
        return
    if type_ == "retrieval":
        for chunk in fields.get("chunks", []):
            if chunk.get("file") and chunk["file"] not in rec.sources:
                rec.sources.append(chunk["file"])
    if type_ == "code_data_access":
        for name in fields.get("files", []):
            if name not in rec.sources:
                rec.sources.append(name)
    if type_ == "plan":
        rec.plan_tools = [s.get("tool", "") for s in (fields.get("task_plan") or {}).get("steps", [])]
    rec.emit(type_, **fields)


def _notify_observer(type_: str, fields: dict) -> None:
    """Pass the event to the run's ``observer`` callable, if any (the UI's side panel, src/source_view.py)."""
    from src import run_context

    observer = run_context.current("observer")
    if not callable(observer):
        return
    try:
        observer(type_, fields)
    except Exception as exc:  # the panel must never break a run
        print(f"⚠️ [TRACE] observer failed on {type_}: {exc}")


def blob(obj: Any) -> Optional[str]:
    rec = current()
    return rec.blob(obj) if rec else None


def compact(value: Any) -> Any:
    """Inline small values; store long strings as blobs."""
    if isinstance(value, str) and len(value) > _INLINE_LIMIT:
        ref = blob({"text": value})
        return {"blob": ref, "chars": len(value)} if ref else value[:_INLINE_LIMIT]
    if isinstance(value, (int, float, bool)) or value is None or isinstance(value, str):
        return value
    if isinstance(value, dict):
        return {str(k): compact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [compact(v) for v in value]
    return repr(value)[:200]


# --------------------------------------------------------------------------- deadlines

def check_deadline(where: str) -> None:
    from src import run_context

    deadline = run_context.current("deadline")
    if deadline is not None and time.monotonic() > float(deadline):
        raise TaskTimeout(f"time budget exhausted before {where}")


def remaining() -> Optional[float]:
    from src import run_context

    deadline = run_context.current("deadline")
    return None if deadline is None else max(0.0, float(deadline) - time.monotonic())


# --------------------------------------------------------------------------- tool decorator

def traced_tool(name: str) -> Callable:
    """Record a ``tool_call`` event (arguments, result summary, duration, exception) for a tool function."""

    def decorate(func: Callable) -> Callable:
        signature = inspect.signature(func)

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if current() is None:
                return func(*args, **kwargs)
            bound = signature.bind_partial(*args, **kwargs)
            depth = _DEPTH.get()
            token = _DEPTH.set(depth + 1)
            started = time.monotonic()
            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                event("tool_call", name=name, depth=depth, args=compact(dict(bound.arguments)), result_summary=None,
                      duration_ms=round((time.monotonic() - started) * 1000), exception=f"{type(exc).__name__}: {exc}")
                raise
            finally:
                _DEPTH.reset(token)
            summary = result if isinstance(result, str) else json.dumps(compact(result), ensure_ascii=False, default=str)
            event("tool_call", name=name, depth=depth, args=compact(dict(bound.arguments)), result_summary=summary[:_INLINE_LIMIT],
                  duration_ms=round((time.monotonic() - started) * 1000), exception=None)
            return result

        return wrapper

    return decorate
