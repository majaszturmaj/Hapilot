#!/usr/bin/env python3
"""Minimal terminal chat with Hapilot (for the GPU host; no UI).

    HAPILOT_MODEL_URL=http://127.0.0.1:8611/v1 .venv/bin/python scripts/chat_terminal.py [--profile P]

The terminal cannot show forms, so it runs with the ``auto`` approval policy: plans run without
approval and documents are generated from the model's parameters directly.
"""

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.privacy import apply_offline_env  # noqa: E402

apply_offline_env()

from langchain_core.messages import HumanMessage  # noqa: E402

from src import run_context  # noqa: E402
from src.agent_graph import get_app  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile")
    args = parser.parse_args()
    app = get_app()
    session = f"terminal-{uuid.uuid4().hex[:8]}"
    messages, last_file = [], ""
    print("Hapilot — terminal. Commands: exit, clear")
    while True:
        try:
            text = input("\n👤 > ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if text in ("exit", "quit"):
            break
        if text == "clear":
            messages, last_file = [], ""
            continue
        if not text:
            continue
        messages.append(HumanMessage(content=text))
        state = app.invoke(
            {"messages": list(messages), "task_plan": {}, "current_step": None, "retry_count": 0, "hitl_request": None,
             "hitl_response": None, "last_generated_file": last_file, "generated_code": None, "halt": False},
            config=run_context.make_config(session, profile=args.profile, approval_policy="auto"))
        messages = list(state["messages"])
        last_file = state.get("last_generated_file") or last_file
        print(f"\n🤖 {messages[-1].content}")
    print(f"\nWorkspace of this session: {run_context.get_settings().workspace_root / session}")


if __name__ == "__main__":
    main()
