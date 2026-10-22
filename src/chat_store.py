"""Chat history for the UI: users, chats (threads), their steps, elements and feedback, in one SQLite file.

``ChatStore`` is plain ``sqlite3`` (no Chainlit, no extra packages); ``src/ui_history.py`` adapts it to
Chainlit's data layer interface. Chainlit's own JSON fields (step and element dicts, thread metadata)
are stored as JSON text.

The agent's own state of a chat — the LangChain message history and the side panel turns — is kept
in ``agent_state`` rather than in Chainlit's thread metadata: that metadata is dropped as a whole once
it exceeds 1 MB, and long attachments or panel turns with full documents would reach that.

Deleting a chat also deletes its workspace files and attachment collection (``delete_chat_files``).

Every user sees only their own chats: ``list_threads`` filters by user, and Chainlit refuses to open a
thread whose ``get_thread_author`` is not the logged-in user.
"""

from __future__ import annotations

import functools
import json
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, identifier TEXT UNIQUE NOT NULL, metadata TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, name TEXT, user_id TEXT, user_identifier TEXT,
    tags TEXT NOT NULL DEFAULT '[]', metadata TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS threads_user ON threads (user_id, created_at);
CREATE TABLE IF NOT EXISTS steps (
    id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, created_at TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS steps_thread ON steps (thread_id, created_at);
CREATE TABLE IF NOT EXISTS elements (
    id TEXT PRIMARY KEY, thread_id TEXT, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS elements_thread ON elements (thread_id);
CREATE TABLE IF NOT EXISTS feedbacks (
    id TEXT PRIMARY KEY, for_id TEXT NOT NULL, thread_id TEXT, value INTEGER NOT NULL, comment TEXT);
CREATE TABLE IF NOT EXISTS agent_state (
    thread_id TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (thread_id, key));
"""


def _atomic(method):
    """Run a store method under the store's lock: Chainlit saves steps concurrently (worker threads), and
    the check-then-write sequences (thread created by the first step, step created or merged) must not interleave."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class ChatStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ users

    @_atomic
    def get_user(self, identifier: str) -> Optional[dict]:
        rows = self._q("SELECT * FROM users WHERE identifier = ?", (identifier,))
        return _user(rows[0]) if rows else None

    @_atomic
    def create_user(self, identifier: str, metadata: Optional[dict] = None) -> dict:
        existing = self.get_user(identifier)
        if existing:
            if metadata is not None:
                self._q("UPDATE users SET metadata = ? WHERE identifier = ?", (json.dumps(metadata), identifier))
                existing["metadata"] = metadata
            return existing
        self._q("INSERT INTO users (id, identifier, metadata, created_at) VALUES (?, ?, ?, ?)",
                (str(uuid.uuid4()), identifier, json.dumps(metadata or {}), now()))
        return self.get_user(identifier)

    # ------------------------------------------------------------------ threads

    @_atomic
    def update_thread(self, thread_id: str, name: Optional[str] = None, user_id: Optional[str] = None,
                      metadata: Optional[dict] = None, tags: Optional[list] = None) -> None:
        """Create the thread if needed; set only the fields that are given (metadata is merged)."""
        rows = self._q("SELECT * FROM threads WHERE id = ?", (thread_id,))
        if not rows:
            self._q("INSERT OR IGNORE INTO threads (id, created_at) VALUES (?, ?)", (thread_id, now()))
            rows = self._q("SELECT * FROM threads WHERE id = ?", (thread_id,))
        row = rows[0]
        sets: dict[str, Any] = {}
        if name is not None:
            sets["name"] = name
        if user_id is not None:
            ident = self._q("SELECT identifier FROM users WHERE id = ?", (user_id,))
            sets["user_id"] = user_id
            sets["user_identifier"] = ident[0]["identifier"] if ident else None
        if metadata is not None:
            sets["metadata"] = json.dumps({**json.loads(row["metadata"] or "{}"), **metadata}, ensure_ascii=False)
        if tags is not None:
            sets["tags"] = json.dumps(tags, ensure_ascii=False)
        if sets:
            self._q(f"UPDATE threads SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?", (*sets.values(), thread_id))

    @_atomic
    def thread_author(self, thread_id: str) -> str:
        rows = self._q("SELECT user_identifier FROM threads WHERE id = ?", (thread_id,))
        return (rows[0]["user_identifier"] or "") if rows else ""

    @_atomic
    def get_thread(self, thread_id: str) -> Optional[dict]:
        rows = self._q("SELECT * FROM threads WHERE id = ?", (thread_id,))
        if not rows:
            return None
        thread = _thread(rows[0])
        feedback = {r["for_id"]: {"id": r["id"], "forId": r["for_id"], "value": r["value"], "comment": r["comment"]}
                    for r in self._q("SELECT * FROM feedbacks WHERE thread_id = ?", (thread_id,))}
        steps = []
        for r in self._q("SELECT data FROM steps WHERE thread_id = ? ORDER BY created_at, rowid", (thread_id,)):
            step = json.loads(r["data"])
            if step.get("id") in feedback:
                step["feedback"] = feedback[step["id"]]
            steps.append(step)
        thread["steps"] = steps
        thread["elements"] = [json.loads(r["data"]) for r in
                              self._q("SELECT data FROM elements WHERE thread_id = ? ORDER BY rowid", (thread_id,))]
        return thread

    @_atomic
    def list_threads(self, user_id: Optional[str], first: int = 20, cursor: Optional[str] = None,
                     search: Optional[str] = None) -> tuple[list[dict], bool]:
        """A user's threads, newest first; (page, has_next_page). Threads without steps are not listed."""
        if not user_id:
            return [], False
        sql = ("SELECT * FROM threads t WHERE user_id = ? AND EXISTS (SELECT 1 FROM steps s WHERE s.thread_id = t.id)")
        args: list[Any] = [user_id]
        if search:
            sql += " AND name LIKE ? ESCAPE '\\'"
            args.append("%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if cursor:
            anchor = self._q("SELECT created_at FROM threads WHERE id = ?", (cursor,))
            if anchor:
                sql += " AND (created_at < ? OR (created_at = ? AND id < ?))"
                args += [anchor[0]["created_at"], anchor[0]["created_at"], cursor]
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(first + 1)
        rows = self._q(sql, tuple(args))
        return [_thread(r) for r in rows[:first]], len(rows) > first

    @_atomic
    def delete_thread(self, thread_id: str) -> None:
        for table, column in (("steps", "thread_id"), ("elements", "thread_id"), ("feedbacks", "thread_id"),
                              ("agent_state", "thread_id"), ("threads", "id")):
            self._q(f"DELETE FROM {table} WHERE {column} = ?", (thread_id,))

    # ------------------------------------------------------------------ steps, elements, feedback

    @_atomic
    def upsert_step(self, step: dict) -> None:
        thread_id = step.get("threadId")
        if not thread_id or not step.get("id"):
            return
        self.update_thread(thread_id)
        rows = self._q("SELECT data, created_at FROM steps WHERE id = ?", (step["id"],))
        if rows:
            merged = {**json.loads(rows[0]["data"]), **{k: v for k, v in step.items() if v is not None}}
            self._q("UPDATE steps SET data = ? WHERE id = ?", (json.dumps(merged, ensure_ascii=False), step["id"]))
        else:
            created = step.get("createdAt") or step.get("start") or now()
            self._q("INSERT INTO steps (id, thread_id, created_at, data) VALUES (?, ?, ?, ?)",
                    (step["id"], thread_id, created, json.dumps(step, ensure_ascii=False)))

    @_atomic
    def delete_step(self, step_id: str) -> None:
        self._q("DELETE FROM steps WHERE id = ?", (step_id,))
        self._q("DELETE FROM feedbacks WHERE for_id = ?", (step_id,))

    @_atomic
    def upsert_element(self, element: dict) -> None:
        if not element.get("id"):
            return
        self._q("INSERT OR REPLACE INTO elements (id, thread_id, data) VALUES (?, ?, ?)",
                (element["id"], element.get("threadId"), json.dumps(element, ensure_ascii=False)))

    @_atomic
    def get_element(self, thread_id: str, element_id: str) -> Optional[dict]:
        rows = self._q("SELECT data FROM elements WHERE id = ? AND thread_id = ?", (element_id, thread_id))
        return json.loads(rows[0]["data"]) if rows else None

    @_atomic
    def delete_element(self, element_id: str) -> None:
        self._q("DELETE FROM elements WHERE id = ?", (element_id,))

    @_atomic
    def upsert_feedback(self, for_id: str, value: int, comment: Optional[str] = None,
                        feedback_id: Optional[str] = None) -> str:
        rows = self._q("SELECT id FROM feedbacks WHERE for_id = ?", (for_id,))
        fid = feedback_id or (rows[0]["id"] if rows else str(uuid.uuid4()))
        thread = self._q("SELECT thread_id FROM steps WHERE id = ?", (for_id,))
        self._q("DELETE FROM feedbacks WHERE for_id = ? OR id = ?", (for_id, fid))
        self._q("INSERT INTO feedbacks (id, for_id, thread_id, value, comment) VALUES (?, ?, ?, ?, ?)",
                (fid, for_id, thread[0]["thread_id"] if thread else None, int(value), comment))
        return fid

    @_atomic
    def delete_feedback(self, feedback_id: str) -> bool:
        existed = bool(self._q("SELECT 1 FROM feedbacks WHERE id = ?", (feedback_id,)))
        self._q("DELETE FROM feedbacks WHERE id = ?", (feedback_id,))
        return existed

    # ------------------------------------------------------------------ agent state of a chat

    @_atomic
    def save_state(self, thread_id: str, key: str, value: Any) -> None:
        self._q("INSERT OR REPLACE INTO agent_state (thread_id, key, data) VALUES (?, ?, ?)",
                (thread_id, key, json.dumps(value, ensure_ascii=False, default=str)))

    @_atomic
    def load_state(self, thread_id: str) -> dict:
        return {r["key"]: json.loads(r["data"]) for r in
                self._q("SELECT key, data FROM agent_state WHERE thread_id = ?", (thread_id,))}


def delete_chat_files(chat_id: str) -> list[str]:
    """Remove a deleted chat's workspace and its attachment collection; returns what was removed."""
    from src import attachments, knowledge_base, run_context
    from src.config import get_settings

    settings = get_settings()
    removed = []
    with run_context.use_context(session_id=chat_id):
        sid = run_context.session_id()
    if sid != chat_id:  # an id that had to be rewritten (e.g. "..", which becomes "default") is not a chat's
        return removed
    root = settings.workspace_root.resolve()
    workspace = (root / sid).resolve()
    if workspace.parent == root and workspace.is_dir():  # never anything outside the workspace root
        shutil.rmtree(workspace)
        removed.append(str(workspace))
    index = settings.data_dir / "attachments_index"
    if index.is_dir():
        name = attachments.collection_name(sid)
        try:
            client = knowledge_base.chroma_client(index)
            if name in [getattr(c, "name", c) for c in client.list_collections()]:
                client.delete_collection(name)
                knowledge_base.reset_cache()
                removed.append(f"{index}:{name}")
        except Exception as exc:  # the chat is gone either way; say what could not be cleaned
            print(f"⚠️ [HISTORY] attachment collection {name}: {exc}")
    return removed


def _user(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "identifier": row["identifier"], "metadata": json.loads(row["metadata"] or "{}"),
            "createdAt": row["created_at"]}


def _thread(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "createdAt": row["created_at"], "name": row["name"], "userId": row["user_id"],
            "userIdentifier": row["user_identifier"], "tags": json.loads(row["tags"] or "[]"),
            "metadata": json.loads(row["metadata"] or "{}"), "steps": [], "elements": []}
