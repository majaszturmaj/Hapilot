"""
tools_common — Shared utilities used across extensions.

Contains:
- TaskState: persistent state for multi-step tasks
- HITL helpers: create_hitl_action_request, create_hitl_form_request, etc.
- invoke_llm_and_normalize: robust LLM invocation wrapper
- Common file utilities
"""

import os
import re
import json
import time
import base64
import inspect
import asyncio
from datetime import datetime
from typing import Optional

from langchain_core.messages import AIMessage, SystemMessage


# --- STATE PERSISTENCE ---
STATE_PERSISTENCE_DIR = os.getenv("STATE_PERSISTENCE_DIR", "./task_states")
os.makedirs(STATE_PERSISTENCE_DIR, exist_ok=True)


class TaskState:
    """Persistent state container for multi-step tasks."""

    def __init__(self, task_id: str):
        self.task_id = task_id
        self.state_file = os.path.join(STATE_PERSISTENCE_DIR, f"{task_id}.json")
        self.data = self._load_state() or {
            "task_id": task_id,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "steps_completed": [],
            "current_step": 0,
            "context": {},
            "outputs": {},
        }

    def _load_state(self) -> Optional[dict]:
        """Load state from file."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return None
        return None

    def save(self):
        """Save state to file."""
        self.data["updated_at"] = datetime.now().isoformat()
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def update_step(self, step_id: int, output: str):
        """Mark a step as completed."""
        self.data["steps_completed"].append(step_id)
        self.data["current_step"] = step_id + 1
        self.data["outputs"][f"step_{step_id}"] = output
        self.save()

    def update_context(self, key: str, value):
        """Store information for subsequent steps."""
        self.data["context"][key] = value
        self.save()

    def get_context(self, key: str = None):
        """Retrieve stored data."""
        if key:
            return self.data["context"].get(key)
        return self.data["context"]

    def cleanup(self):
        """Remove state after completion."""
        if os.path.exists(self.state_file):
            os.remove(self.state_file)


# --- HITL (Human-in-the-Loop) HELPERS ---

def create_hitl_action_request(content: str, actions: list, continuation: dict = None) -> dict:
    """Create a HITL request that presents selectable options to the user.

    Args:
        content: Message shown to the user explaining what to choose.
        actions: List of dicts with keys: name (str), label (str), payload (dict).
        continuation: Dict describing how to continue after user responds.
    """
    return {
        "type": "action",
        "content": content,
        "actions": actions,
        "continuation": continuation or {},
    }


def create_hitl_form_request(content: str, fields: list, continuation: dict = None) -> dict:
    """Create a HITL request that presents a fillable form to the user.

    Args:
        content: Message shown above the form.
        fields: List of form field dicts with keys:
            - id (str): field identifier
            - label (str): display label
            - type (str): "text" | "textarea" | "select"
            - value (str): pre-filled value from the agent
            - options (list[str]): choices for "select" type
            - required (bool): whether the field is required
        continuation: Dict describing how to continue after user responds.
    """
    return {
        "type": "form",
        "content": content,
        "fields": fields,
        "continuation": continuation or {},
    }


def create_hitl_confirm_request(content: str, continuation: dict = None) -> dict:
    """Create a HITL request for simple confirmation (approve/reject/edit).

    Args:
        content: Message with preview of what will be done, asking user to confirm.
        continuation: Dict describing how to continue after user responds.
    """
    return {
        "type": "confirm",
        "content": content,
        "continuation": continuation or {},
    }


# --- LLM INVOCATION ---

def invoke_llm_and_normalize(llm, messages):
    """Invoke an LLM and normalize the result to a message-like object.

    Handles several adapter return shapes: direct message objects, dicts,
    streaming iterators (collects chunks), and awaitables (best-effort).
    Returns an object with a `content` attribute (AIMessage/SystemMessage
    or similar). On irrecoverable errors returns a simple AIMessage-like
    object containing the error text.
    """
    try:
        resp = llm.invoke(messages)
    except Exception as e:
        try:
            return AIMessage(content=f"[LLM INVOKE ERROR] {e}")
        except Exception:
            return SystemMessage(content=f"[LLM INVOKE ERROR] {e}")

    # If the adapter returned an awaitable/coroutine, try to resolve it
    if inspect.isawaitable(resp):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                return AIMessage(content=f"[LLM] returned awaitable while loop running: {type(resp)}")
            else:
                resp = loop.run_until_complete(resp)
        except Exception as e:
            return AIMessage(content=f"[LLM AWAIT ERROR] {e}")

    # PRIORITY: If it already has a content attribute (AIMessage, SystemMessage, etc.),
    # return it directly. This MUST come before the iterator check because
    # LangChain message objects are iterable (yield field tuples) but we want .content.
    if hasattr(resp, "content") and isinstance(resp.content, str):
        return resp

    # If it's a dict-like response, extract text
    if isinstance(resp, dict):
        content = resp.get("content") or resp.get("text") or str(resp)
        return AIMessage(content=content)

    # If it's an iterator (streaming), collect chunks
    try:
        if hasattr(resp, "__iter__") and not isinstance(resp, (str, bytes, dict)):
            parts = []
            for chunk in resp:
                if hasattr(chunk, "content"):
                    parts.append(chunk.content)
                elif isinstance(chunk, dict):
                    parts.append(chunk.get("content") or chunk.get("text") or str(chunk))
                else:
                    parts.append(str(chunk))
            return AIMessage(content="".join(parts))
    except Exception:
        pass

    # Fallback: stringify
    return AIMessage(content=str(resp))


# --- FILE UTILITIES ---

def generate_unique_filename(prefix: str = "raport") -> str:
    """Generate unique filenames with a timestamp."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}.tex"
