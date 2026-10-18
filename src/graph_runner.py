"""Run the agent graph and stream node updates to an async consumer (the Chainlit UI, scripts).

The graph runs in a worker thread; each node update is put on an asyncio queue. The worker always
puts a final sentinel — also when the graph raises — so a consumer can never wait forever.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

ERROR_PREFIX = "Przepraszam, wystąpił błąd podczas przetwarzania zapytania"


@dataclass
class GraphRun:
    events: list = field(default_factory=list)   # [(node_name, output_dict)]
    error: Optional[str] = None


async def run_graph_streaming(app: Any, inputs: dict, config: dict,
                              on_event: Optional[Callable[[str, dict], Awaitable[None]]] = None) -> GraphRun:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    run = GraphRun()

    def worker() -> None:
        try:
            for update in app.stream(inputs, config=config, stream_mode="updates"):
                for node_name, output in (update or {}).items():
                    if node_name != "__end__":
                        loop.call_soon_threadsafe(queue.put_nowait, ("event", node_name, dict(output or {})))
        except BaseException as exc:  # report every failure to the consumer
            loop.call_soon_threadsafe(queue.put_nowait, ("error", type(exc).__name__, str(exc)))
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    future = loop.run_in_executor(None, worker)
    while True:
        item = await queue.get()
        if item is None:
            break
        kind, name, payload = item
        if kind == "error":
            run.error = f"{name}: {payload}"
            continue
        run.events.append((name, payload))
        if on_event is not None:
            try:
                await on_event(name, payload)
            except Exception as exc:  # a UI problem must not stop draining the queue
                print(f"⚠️ [RUNNER] on_event failed: {exc}")
    await future
    return run


def extract_hitl_request(events: list) -> Optional[dict]:
    for _name, output in reversed(events):
        request = output.get("hitl_request")
        if isinstance(request, dict) and request:
            return request
    return None


def new_messages(events: list) -> list:
    messages = []
    for _name, output in events:
        messages.extend(output.get("messages") or [])
    return messages


def final_text(run: GraphRun) -> str:
    """The text to show the user: the last message, or an explanation of the failure."""
    messages = new_messages(run.events)
    if run.error:
        return f"{ERROR_PREFIX}: {run.error}. Spróbuj ponownie lub sformułuj zapytanie inaczej."
    if not messages:
        return "Przepraszam, nie udało mi się przetworzyć tego zapytania. Opisz, czego potrzebujesz, a spróbujemy inaczej."
    last = messages[-1]
    return getattr(last, "content", str(last))


def last_generated_file(events: list) -> Optional[str]:
    for _name, output in reversed(events):
        if output.get("last_generated_file"):
            return output["last_generated_file"]
    return None


def generated_pdf_name(text: str) -> Optional[str]:
    """The PDF file name announced by the document tools ("Wygenerowano plik: x.pdf")."""
    match = re.search(r"Wygenerowano plik:\s*(\S+\.pdf)", text or "")
    return match.group(1) if match else None
