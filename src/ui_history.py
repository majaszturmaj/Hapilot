"""Chainlit data layer on top of src/chat_store.py: chat history per logged-in user.

Enabled by src/app.py only when login is on (``HAPILOT_UI_USERS_FILE``); Chainlit shows past chats in
the left sidebar and calls ``on_chat_resume`` when one is opened. The database is
``HAPILOT_DATA_DIR/chat_history.sqlite`` (``HAPILOT_CHAT_DB`` overrides it).

Elements: downloadable files (``cl.File`` of generated documents) are stored with their workspace path
and served again through the side panel's token route (src/ui_panel.py); custom elements (the
document form, the side panel) are not stored — a resumed chat gets its panel from the agent state.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Dict, List, Optional

from chainlit.data.base import BaseDataLayer
from chainlit.data.utils import queue_until_user_message
from chainlit.types import Feedback, PageInfo, PaginatedResponse, Pagination, ThreadDict, ThreadFilter
from chainlit.user import PersistedUser, User

from src import ui_panel
from src.chat_store import ChatStore, delete_chat_files
from src.config import get_settings


def db_path() -> Path:
    value = os.environ.get("HAPILOT_CHAT_DB", "").strip()
    return Path(value).expanduser() if value else get_settings().data_dir / "chat_history.sqlite"


class HistoryDataLayer(BaseDataLayer):
    def __init__(self, store: ChatStore):
        self.store = store

    async def _run(self, fn, *args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)

    # ------------------------------------------------------------------ users

    async def get_user(self, identifier: str) -> Optional[PersistedUser]:
        user = await self._run(self.store.get_user, identifier)
        return _persisted(user) if user else None

    async def create_user(self, user: User) -> Optional[PersistedUser]:
        return _persisted(await self._run(self.store.create_user, user.identifier, user.metadata))

    # ------------------------------------------------------------------ feedback

    async def upsert_feedback(self, feedback: Feedback) -> str:
        return await self._run(self.store.upsert_feedback, feedback.forId, feedback.value, feedback.comment, feedback.id)

    async def delete_feedback(self, feedback_id: str) -> bool:
        return await self._run(self.store.delete_feedback, feedback_id)

    # ------------------------------------------------------------------ elements

    @queue_until_user_message()
    async def create_element(self, element):
        if element.type == "custom" or not element.thread_id:
            return
        data = element.to_dict()
        data["path"] = str(element.path) if element.path else None
        data["url"] = None
        await self._run(self.store.upsert_element, data)

    async def get_element(self, thread_id: str, element_id: str):
        element = await self._run(self.store.get_element, thread_id, element_id)
        return _with_url(element, thread_id) if element else None

    @queue_until_user_message()
    async def delete_element(self, element_id: str, thread_id: Optional[str] = None):
        await self._run(self.store.delete_element, element_id)

    # ------------------------------------------------------------------ steps

    @queue_until_user_message()
    async def create_step(self, step_dict):
        await self._run(self.store.upsert_step, dict(step_dict))

    @queue_until_user_message()
    async def update_step(self, step_dict):
        await self._run(self.store.upsert_step, dict(step_dict))

    @queue_until_user_message()
    async def delete_step(self, step_id: str):
        await self._run(self.store.delete_step, step_id)

    # ------------------------------------------------------------------ threads

    async def get_thread_author(self, thread_id: str) -> str:
        return await self._run(self.store.thread_author, thread_id)

    async def delete_thread(self, thread_id: str):
        await self._run(self.store.delete_thread, thread_id)
        ui_panel.forget_session(thread_id)
        await self._run(delete_chat_files, thread_id)

    async def list_threads(self, pagination: Pagination, filters: ThreadFilter) -> PaginatedResponse[ThreadDict]:
        threads, has_next = await self._run(self.store.list_threads, filters.userId, pagination.first,
                                            pagination.cursor, filters.search)
        return PaginatedResponse(data=threads, pageInfo=PageInfo(
            hasNextPage=has_next, startCursor=threads[0]["id"] if threads else None,
            endCursor=threads[-1]["id"] if threads else None))

    async def get_thread(self, thread_id: str) -> Optional[ThreadDict]:
        thread = await self._run(self.store.get_thread, thread_id)
        if thread:
            thread["elements"] = [e for e in (_with_url(e, thread_id) for e in thread["elements"]) if e]
        return thread

    async def update_thread(self, thread_id: str, name: Optional[str] = None, user_id: Optional[str] = None,
                            metadata: Optional[Dict] = None, tags: Optional[List[str]] = None):
        await self._run(self.store.update_thread, thread_id, name, user_id, metadata, tags)

    # ------------------------------------------------------------------ misc

    async def get_favorite_steps(self, user_id: str) -> List:
        return []

    async def build_debug_url(self) -> str:
        return ""

    async def close(self) -> None:
        pass


def _persisted(user: dict) -> PersistedUser:
    return PersistedUser(id=user["id"], identifier=user["identifier"], createdAt=user["createdAt"],
                         metadata=user.get("metadata") or {})


def _with_url(element: dict, thread_id: str) -> Optional[dict]:
    """A stored file element with a fresh download URL; None when the file is gone."""
    path = Path(element.get("path") or "")
    if not element.get("path") or not path.is_file():
        return None
    return {**element, "url": ui_panel.url_for(thread_id, path), "chainlitKey": None}
