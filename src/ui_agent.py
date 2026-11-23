"""Chat with the v3 engine (HAPILOT_ENGINE=v3): app.py hands each user message here.

What the user sees for one message:
  ⏳ a note while the request waits for the model (other users' requests run first), updated with its place in line
  "kroki zadania"   one step per tool call of the agent (reading files, commands, search, PDF, working days, …),
                    with its input and a short piece of its output; text the agent writes before a tool call becomes a
                    "💬" step too, so only the final answer stays a message
  the answer        streamed as the model writes it; files the agent left in /praca/wyniki are attached below it: a
                    PDF by its name in the answer, which opens it in the right-hand panel; other files as downloads
  📋 the PDF form    a PDF the agent prepares is a draft: after the answer the v2 document form shows its values, and
                    "Generuj PDF" makes it (src/agent_documents.py, src/app.py) — no model call; "✏️ Edytuj dane i
                    wygeneruj ponownie" under the PDF opens the form again for a new version
  📎 the side panel  "Źródła i pliki" as in engine v2 (src/ui_panel.py): the documents the answer names, the agent read
                    or its searches returned, shown whole (PDF pages, search fragments highlighted), and the new files
                    (a PDF as pages); it opens after the answer, and the button under the answer opens it again
  ❓ / 📧            the agent's questions (buttons for its options, or free text) and approvals (an e-mail draft:
                    the draft's recipients, subject and text, ✅ / ❌), in the user's own chat
  ⏹                 Chainlit's stop button aborts the turn; the conversation so far is kept
  🔁 opened again    a chat opened again while its turn runs (the user went to another chat and came back: Chainlit
                    gives the browser a new session, and the turn keeps sending to the old one) shows the turn as
                    running there too: the progress indicator and ⏹, a "still working" note, the steps, the answer
                    as it streams, the agent's questions (answered in either session) and the side panel

The model-side history of a chat (the SDK's messages) is saved with the chat (agent_state ``v3_messages``), so a
resumed chat continues where it was; the chat's workspace (src/agent_workspace.py) keeps its files.

Chainlit is imported inside the functions: the helpers above the UI code are plain and unit-tested without it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Awaitable, Callable, Optional

from src.agent_engine import (NO_ANSWER, QUESTION_TIMEOUT_S, TOOL_LABELS, EngineBusy, Turn, TurnResult, chat_prompt,
                              engine)
from src.agent_workspace import ROOT, Workspace

STATE_KEY = "v3_messages"
SHOW_INPUT, SHOW_OUTPUT, SHOW_NOTE = 1500, 1500, 4000
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

STATUS_NOTES = {
    "timeout": "⚠️ Przekroczony czas na odpowiedź — zadanie przerwane. Spróbuj podzielić je na mniejsze kroki.",
    "error": "⚠️ Nie udało mi się dokończyć zadania",
    "aborted": "⏹ Przerwano na Twoją prośbę.",
}


# --------------------------------------------------------------------------- plain helpers (unit-tested)

def tool_label(name: Optional[str]) -> str:
    return TOOL_LABELS.get(name or "", f"🔧 {name or 'narzędzie'}")


def _short(path: str) -> str:
    return path[len(ROOT) + 1:] if path.startswith(ROOT + "/") else path


def _paths(value) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        if value.startswith(ROOT + "/") or value == ROOT:
            found.append(_short(value))
    elif isinstance(value, dict):
        for v in value.values():
            found += _paths(v)
    elif isinstance(value, list):
        for v in value:
            found += _paths(v)
    return found


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " …"


def describe_input(tool: Optional[str], raw: str) -> str:
    """The tool call's input as the user reads it: commands as code, a search query, file paths, else short JSON."""
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return f"```text\n{_clip(raw, SHOW_INPUT)}\n```"
    if isinstance(data, dict):
        if isinstance(data.get("commands"), list):
            commands = "\n".join(str(c) if not isinstance(c, dict) else str(c.get("command", c)) for c in data["commands"])
            return f"```bash\n{_clip(commands, SHOW_INPUT)}\n```"
        if tool == "semantic_search" and data.get("query"):
            return f"„{_clip(str(data['query']), SHOW_INPUT)}”"
        if tool == "email_draft":
            return "szkic e-maila (treść w pytaniu o zgodę)"
        if tool == "pdf_document":
            return f"{data.get('action', '?')} {data.get('template_id') or ''}".strip()
        paths = _paths(data)
        if paths and tool in ("read_files", "editor", "apply_patch", "search_codebase"):
            extra = f"\n\nwzorzec: `{data['pattern']}`" if data.get("pattern") else ""
            return "\n".join(f"- `{p}`" for p in dict.fromkeys(paths)) + extra
    text = json.dumps(data, ensure_ascii=False)
    return f"```json\n{_clip(text, SHOW_INPUT)}\n```"


def describe_output(output: str, error: Optional[str]) -> str:
    if error:
        return f"⚠️ {_clip(str(error), SHOW_OUTPUT)}"
    text = (output or "").strip()
    if text.startswith('"') and text.endswith('"'):
        try:
            text = json.loads(text)
        except ValueError:
            pass
    return f"```text\n{_clip(str(text), SHOW_OUTPUT)}\n```" if text else "(brak wyniku)"


def queue_note(position: int) -> str:
    ahead = "1 zapytanie" if position == 1 else f"{position} zapytania" if position < 5 else f"{position} zapytań"
    return (f"⏳ Model odpowiada teraz komuś innemu. Przed Tobą: **{ahead}**. "
            "Zacznę automatycznie — możesz zostawić to okno otwarte.")


def approval_text(tool: str, args: dict) -> str:
    if tool == "email_draft":
        body = _clip(str(args.get("body", "")), 3000).replace("\n", "\n> ")
        return ("📧 **Hapilot chce utworzyć szkic e-maila** w Twojej skrzynce (nie wyśle go).\n\n"
                f"**Do:** {args.get('to', '')}\n\n**Temat:** {args.get('subject', '')}\n\n> {body}\n\nZgadzasz się?")
    return f"🔐 Hapilot chce użyć narzędzia **{tool}**. Zgadzasz się?"


def answer_text(result: TurnResult, streamed: str) -> str:
    """The answer to show: the runner's final text (else what was streamed), plus a note when the turn failed. After
    a stop only what was being streamed counts: the runner's last text is then often a working note."""
    text = (streamed if result.status == "aborted" else result.text or streamed or "").strip()
    note = STATUS_NOTES.get(result.status)
    if result.status == "error":
        note = f"{note}: {result.error}" if result.error else note
    if note:
        text = f"{text}\n\n{note}" if text else note
    return text or "(brak odpowiedzi)"


def is_pdf(name: str) -> bool:
    return str(name).lower().endswith(".pdf")


def file_list(files: list) -> str:
    """The note under the answer. A PDF is named: Chainlit turns the name of a side element into a link that opens
    it in the right-hand panel, so the name must be in the text."""
    if not files:
        return ""
    pdfs = [Path(f["name"]).name for f in files if is_pdf(f["name"])]
    others = len(files) - len(pdfs)
    parts = []
    if pdfs:
        parts.append("📄 Kliknij nazwę, aby otworzyć dokument po prawej: " + ", ".join(pdfs))
    if others:
        parts.append("⬇️ **" + ("Plik do pobrania poniżej." if others == 1 else "Pliki do pobrania poniżej.") + "**")
    return "\n\n" + "\n\n".join(parts)


# --------------------------------------------------------------------------- a chat opened again during its turn

# Found on the pilot (2026-11-23): the user wrote a message, went to another chat and came back at once. The chat came
# back without the progress indicator and the answer never appeared, though the turn finished and the answer was saved:
# Chainlit gives the returning browser a new websocket session, and everything the running turn sends goes to the
# session where the message was written. Now the turn's sending (its Chainlit emitter) is mirrored to every session
# that opens the chat again while it runs. Plain classes: tested with stand-in emitters (tests/unit/test_v3_rejoin.py).

MIRRORED_CALLS = frozenset({"send_step", "update_step", "delete_step", "send_element", "stream_start", "send_token",
                            "task_start", "task_end", "update_token_count"})
MIRRORED_EVENTS = frozenset({"action", "remove_action", "remove_element", "set_sidebar_elements",
                             "set_sidebar_title"})
_STREAM_CALLS = frozenset({"stream_start", "send_token"})
_ASK = "__ask__"
REJOIN_NOTE = ("⏳ Wciąż pracuję nad odpowiedzią na Twoją poprzednią wiadomość w tej rozmowie. Kolejne kroki "
               "i odpowiedź pojawią się tutaj.")
REJOIN_FALLBACK_S = 5.0  # when the resumed chat's history was sent without the hook (another Chainlit version)
PUMP_IDLE_S = 30.0  # a watcher's sender ends this long after the turn closed, if Chainlit's task_end never came


def _token(args: tuple, kwargs: dict) -> tuple:
    """(id, token, is_sequence, is_input) of an emitter's send_token call, positional or by name."""
    names = ("id", "token", "is_sequence", "is_input")
    values = dict(zip(names, args), **kwargs)
    return values.get("id"), values.get("token") or "", bool(values.get("is_sequence")), bool(values.get("is_input"))


class Watcher:
    """A session that opened the chat during the turn. Until its history is on the screen (``ready``) what the turn
    sends is kept in ``buffer``; then one sender task (``pump``) passes it on in order."""

    def __init__(self, emitter):
        self.emitter = emitter
        self.note: Optional[dict] = None
        self.ready = False
        self.buffer: list = []
        self.queue: Optional[asyncio.Queue] = None
        self.pump: Optional[asyncio.Task] = None


class LiveTurn:
    """A running v3 turn and the sessions watching it."""

    def __init__(self):
        self.watchers: list[Watcher] = []
        self.streaming: dict = {}  # message id → the streamed message so far (for a watcher that comes mid-stream)
        self.closed = False
        self.ask: Optional[tuple] = None  # (step_dict, spec) while the agent's question waits for the user
        self.ask_tasks: list = []  # [(emitter, task)]
        self._changed: Optional[asyncio.Event] = None

    # what the turn sends
    def mirror(self, name: str, args: tuple, kwargs: dict) -> None:
        """Called after the turn's own session got ``name(*args, **kwargs)``: track the stream, pass it on."""
        if name == "stream_start":
            step = dict(args[0] if args else kwargs.get("step_dict") or {})
            step["output"] = step.get("output") or ""
            if step.get("id"):
                self.streaming[step["id"]] = step
        elif name == "send_token":
            sid, token, sequence, is_input = _token(args, kwargs)
            if sid in self.streaming and not is_input:
                step = self.streaming[sid]
                step["output"] = token if sequence else step["output"] + token
        elif name in ("send_step", "update_step", "delete_step"):
            step = args[0] if args else kwargs.get("step_dict") or {}
            self.streaming.pop(step.get("id"), None)
        for watcher in list(self.watchers):
            self._put(watcher, (name, args, kwargs))
        if name == "task_end" and self.closed:  # Chainlit's last word on the turn: the senders can stop
            for watcher in list(self.watchers):
                self._put(watcher, None)

    def _put(self, watcher: Watcher, item) -> None:
        if watcher.ready:
            watcher.queue.put_nowait(item)
        elif item is not None:
            watcher.buffer.append(item)

    # a session comes
    def add(self, emitter) -> Watcher:
        watcher = Watcher(emitter)
        self.watchers.append(watcher)
        return watcher

    def start(self, watcher: Watcher, note: Optional[dict] = None) -> None:
        """The watcher's history is on its screen: show the turn as running, then what came since it opened the chat.
        Synchronous on purpose: nothing the turn sends can slip between the stream snapshot and the queue."""
        if watcher.ready or watcher not in self.watchers:
            return
        items: list = []
        if not self.closed:
            items.append(("task_start", (), {}))
            if note is not None:
                watcher.note = note
                items.append(("send_step", (note,), {}))
        items += [item for item in watcher.buffer if item[0] not in _STREAM_CALLS]  # the snapshot has the streams
        items += [("stream_start", (dict(step),), {}) for step in self.streaming.values()]
        if self.ask is not None:
            items.append((_ASK, (), {}))
        watcher.buffer, watcher.ready, watcher.queue = [], True, asyncio.Queue()
        for item in items:
            watcher.queue.put_nowait(item)
        watcher.pump = asyncio.ensure_future(self._pump(watcher))

    async def _pump(self, watcher: Watcher) -> None:
        while True:
            try:
                item = await asyncio.wait_for(watcher.queue.get(), PUMP_IDLE_S)
            except asyncio.TimeoutError:
                if self.closed:
                    return
                continue
            if item is None:
                return
            name, args, kwargs = item
            if name == _ASK:
                self._ask_watcher(watcher)
                continue
            try:
                await getattr(watcher.emitter, name)(*args, **kwargs)
            except Exception as exc:  # a gone session must not stop the turn
                print(f"⚠️ [V3 REJOIN] {name}: {type(exc).__name__}: {exc}")
                if watcher in self.watchers:
                    self.watchers.remove(watcher)
                return

    def close(self) -> None:
        """The turn's answer is out: take the notes away; Chainlit's task_end (mirrored) ends the progress indicator."""
        self.closed = True
        for watcher in list(self.watchers):
            if watcher.note is not None:
                self._put(watcher, ("delete_step", (watcher.note,), {}))

    # the agent's questions
    def _ask_watcher(self, watcher: Watcher) -> None:
        if self.ask is None:
            return
        step_dict, spec = self.ask
        task = asyncio.ensure_future(watcher.emitter.send_ask_user(step_dict, spec, False))
        self.ask_tasks.append((watcher.emitter, task))
        if self._changed is not None:
            self._changed.set()

    async def ask_all(self, inner, step_dict, spec, raise_on_timeout: bool = False):
        """The question goes to the turn's session and to every watcher, also one that comes while it waits; the first
        answer wins and the others are taken off their screens."""
        self.ask, self._changed = (step_dict, spec), asyncio.Event()
        self.ask_tasks = [(inner, asyncio.ensure_future(inner.send_ask_user(step_dict, spec, False)))]
        for watcher in list(self.watchers):
            self._put(watcher, (_ASK, (), {}))
        answer = None
        try:
            while answer is None:
                for _emitter, task in self.ask_tasks:
                    if task.done() and not task.cancelled() and task.exception() is None and task.result():
                        answer = task.result()
                        break
                if answer is not None:
                    break
                pending = [task for _emitter, task in self.ask_tasks if not task.done()]
                if not pending:
                    break
                self._changed.clear()
                woken = asyncio.ensure_future(self._changed.wait())
                await asyncio.wait([*pending, woken], return_when=asyncio.FIRST_COMPLETED)
                woken.cancel()
        finally:
            tasks, self.ask, self.ask_tasks, self._changed = self.ask_tasks, None, [], None
            for emitter, task in tasks:
                if not task.done():
                    task.cancel()
                    try:
                        await emitter.clear("clear_ask")
                    except Exception:
                        pass
        if answer is None and raise_on_timeout:
            raise TimeoutError("no answer")
        return answer


class MirrorEmitter:
    """The turn's Chainlit emitter: everything the turn's session gets, the watchers get too."""

    def __init__(self, inner, live: LiveTurn):
        self._inner, self._live = inner, live

    def __getattr__(self, name: str):
        attr = getattr(self._inner, name)
        live = self._live
        if name in MIRRORED_CALLS:
            async def call(*args, **kwargs):
                result = await attr(*args, **kwargs)
                live.mirror(name, args, kwargs)
                return result
            return call
        if name == "emit":
            async def emit(event, data, *rest):
                result = await attr(event, data, *rest)
                if event in MIRRORED_EVENTS:
                    live.mirror("emit", (event, data), {})
                return result
            return emit
        if name == "send_ask_user":
            async def ask(step_dict, spec, raise_on_timeout=False):
                return await live.ask_all(self._inner, step_dict, spec, raise_on_timeout)
            return ask
        return attr


class ResumeHook:
    """The returning session's emitter while Chainlit resumes the chat: once the history is sent (resume_thread,
    after on_chat_resume), the watcher starts — sooner its messages would be wiped by the history."""

    def __init__(self, inner, after: Callable[[], None]):
        self._inner, self._after = inner, after

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def resume_thread(self, thread):
        result = await self._inner.resume_thread(thread)
        self._after()
        return result


class KeepRunning:
    """An emitter whose task_end is skipped while ``running()`` is true."""

    def __init__(self, inner, running: Callable[[], bool]):
        self._inner, self._running = inner, running

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def task_end(self):
        if not self._running():
            return await self._inner.task_end()


_LIVE: dict[str, LiveTurn] = {}


# --------------------------------------------------------------------------- Chainlit

class ChainlitTurn:
    """The engine's TurnUI for one user message (src/agent_engine.py)."""

    def __init__(self, author: str, save):
        import chainlit as cl

        self.cl = cl
        self.author = author
        self.save = save  # save(messages) — the chat's model-side history
        self.parent = None
        self.queue_message = None
        self.answer = None  # the message being streamed
        self.streamed = ""
        self.steps: dict = {}

    async def start(self) -> None:
        """The "kroki zadania" step, when the turn starts (after the queue); shown as running until it ends."""
        from chainlit.utils import utc_now

        self.parent = self.cl.Step(name="kroki zadania", type="run", show_input=False)
        self.parent.start = utc_now()
        await self.parent.send()

    async def finish_steps(self, summary: str) -> None:
        if self.parent is not None:
            from chainlit.utils import utc_now

            self.parent.output = summary
            self.parent.end = utc_now()
            await self.parent.update()

    async def queued(self, position: int) -> None:
        if position == 0 and self.parent is None:
            await self.start()
        if position > 0:
            if self.queue_message is None:
                self.queue_message = self.cl.Message(content=queue_note(position), author=self.author)
                await self.queue_message.send()
            else:
                self.queue_message.content = queue_note(position)
                await self.queue_message.update()
        elif self.queue_message is not None:
            await self.queue_message.remove()
            self.queue_message = None

    async def _step(self, name: str, output: str = "", done: bool = False):
        """A child step of "kroki zadania"; its text goes in the output, which Chainlit renders as Markdown."""
        from chainlit.utils import utc_now

        step = self.cl.Step(name=name, type="tool", parent_id=self.parent.id if self.parent else None,
                            show_input=False)
        step.start = utc_now()
        step.output = output
        if done:
            step.end = step.start
        await step.send()
        return step

    async def _note_from_stream(self) -> None:
        """Text the agent wrote before a tool call is a working note, not the answer: move it into the steps."""
        if self.answer is None:
            return
        text = self.streamed.strip()
        await self.answer.remove()
        self.answer, self.streamed = None, ""
        if text:
            await self._step("💬 Notatka", _clip(text, SHOW_NOTE), done=True)

    async def event(self, event: dict) -> None:
        kind = event.get("ev")
        if kind == "text_delta":
            delta = event.get("text") or ""
            if not delta:
                return
            if self.answer is None:
                self.answer = self.cl.Message(content="", author=self.author)
            self.streamed += delta
            await self.answer.stream_token(delta)
        elif kind == "tool_start":
            await self._note_from_stream()
            described = describe_input(event.get("tool"), event.get("input") or "")
            step = await self._step(tool_label(event.get("tool")), described)
            self.steps[event.get("id")] = (step, described)
        elif kind == "tool_end":
            from chainlit.utils import utc_now

            step, described = self.steps.pop(event.get("id"), (None, ""))
            if step is None:
                step = await self._step(tool_label(event.get("tool")))
            result = describe_output(event.get("output") or "", event.get("error"))
            step.output = f"{described}\n\n**Wynik:**\n\n{result}" if described else result
            step.end = utc_now()
            await step.update()
        elif kind == "notice" and event.get("reason") in ("mistake_limit", "auto_compaction", "api_error"):
            await self._step("ℹ️ Komunikat", _clip(str(event.get("message") or ""), SHOW_NOTE), done=True)

    async def ask(self, question: str, options: list) -> str:
        cl = self.cl
        text = f"❓ {question}"
        if options:
            other = "✍️ Inna odpowiedź"
            actions = [cl.Action(name="v3_odpowiedz", label=_clip(o, 80), payload={"value": o}) for o in options]
            actions.append(cl.Action(name="v3_odpowiedz", label=other, payload={"value": None}))
            ask = cl.AskActionMessage(content=text, actions=actions, author=self.author, timeout=QUESTION_TIMEOUT_S)
            res = await ask.send()
            value = (res or {}).get("payload", {}).get("value") if res else None
            ask.content = f"{text}\n\n**Odpowiedź:** {value or (other if res else '— (brak)')}"
            await ask.update()
            if res is None:
                return NO_ANSWER
            if value is not None:
                return str(value)
            text = "✍️ Napisz swoją odpowiedź:"
        res = await cl.AskUserMessage(content=text, author=self.author, timeout=QUESTION_TIMEOUT_S).send()
        return str((res or {}).get("output") or "").strip() or NO_ANSWER

    async def approve(self, tool: str, args: dict) -> tuple[bool, str]:
        cl = self.cl
        actions = [cl.Action(name="v3_zgoda", label="✅ Zgoda", payload={"ok": True}),
                   cl.Action(name="v3_zgoda", label="❌ Odmowa", payload={"ok": False})]
        ask = cl.AskActionMessage(content=approval_text(tool, args), actions=actions, author=self.author,
                                  timeout=QUESTION_TIMEOUT_S)
        res = await ask.send()
        ok = bool(res and (res.get("payload") or {}).get("ok"))
        ask.content = approval_text(tool, args) + ("\n\n**✅ Zgoda**" if ok else "\n\n**❌ Bez zgody**")
        await ask.update()
        return (True, "") if ok else (False, "użytkownik nie wyraził zgody" if res else "brak odpowiedzi użytkownika")

    async def aborted(self, result: TurnResult) -> None:
        if result.messages is not None:
            await self.cl.make_async(self.save)(result.messages)
        await self.finish_steps("⏹ przerwano")
        await self.deliver(result)

    async def deliver(self, result: TurnResult):
        """Send the answer with its files; returns the message (the side panel's button goes under it)."""
        cl = self.cl
        elements = []
        for f in result.files:
            elements.append(file_element(cl, Path(f["name"]).name, f["path"]))
        content = answer_text(result, self.streamed) + file_list(result.files)
        message, self.answer = self.answer, None
        if message is not None:
            message.content = content
            message.elements = elements
            await message.send()  # ends the stream and saves the message
        else:
            message = cl.Message(content=content, elements=elements, author=self.author)
            await message.send()
        return message


def file_element(cl, name: str, path):
    """A result file under a message: a PDF opens in the right-hand panel (its name in the text is the link), a
    picture is shown, anything else is a download."""
    if is_pdf(name):
        return cl.Pdf(name=name, path=str(path), display="side")
    kind = cl.Image if Path(name).suffix.lower() in IMAGE_TYPES else cl.File
    return kind(name=name, path=str(path), display="inline")


# --------------------------------------------------------------------------- entry points for app.py

def load_history(store, chat_id: str) -> list:
    if store is None:
        import chainlit as cl

        return cl.user_session.get(STATE_KEY) or []
    return (store.load_state(chat_id) or {}).get(STATE_KEY) or []


def save_history(store, chat_id: str, messages: list) -> None:
    if store is None:
        import chainlit as cl

        cl.user_session.set(STATE_KEY, messages)
        return
    store.save_state(chat_id, STATE_KEY, messages)


def _summary(result: TurnResult) -> str:
    calls = sum(result.tool_calls.values())
    names = ", ".join(f"{tool_label(k)} ×{v}" for k, v in sorted(result.tool_calls.items(), key=lambda kv: -kv[1]))
    return f"**Kroków:** {calls} · **czas:** {result.duration_s:.0f} s" + (f"\n\n{names}" if names else "")


PanelHook = Callable[[dict, object], Awaitable[None]]
DocumentHook = Callable[[str], Awaitable[None]]


def panel_turn(chat_id: str, question: str, result: TurnResult) -> Optional[dict]:
    """The side panel's data for a finished v3 turn (None: no documents, no files)."""
    from src import ui_panel

    return ui_panel.build_agent_turn(chat_id, question, answer_text(result, ""), result.files, result.retrievals,
                                     result.opened, Workspace(chat_id).uploads)


async def on_message(message, *, chat_id: str, user: Optional[str], author: str, store,
                     panel: Optional[PanelHook] = None, document: Optional[DocumentHook] = None) -> None:
    """``panel(turn, answer_message)`` keeps and shows the side panel's turn; ``document(draft_id)`` shows the form
    of a PDF draft and makes the PDF (both src/app.py)."""
    import chainlit as cl

    eng = engine()
    if eng.busy(chat_id):
        await cl.Message(content="⏳ Jeszcze pracuję nad poprzednim zadaniem w tej rozmowie. Zatrzymaj je (⏹) albo "
                                 "poczekaj na odpowiedź.", author=author).send()
        _keep_running(chat_id)
        return
    live = LiveTurn()
    _LIVE[chat_id] = live
    _mirror(live)
    try:
        await _turn(message, eng, chat_id=chat_id, user=user, author=author, store=store, panel=panel,
                    document=document)
    finally:
        if _LIVE.get(chat_id) is live:
            del _LIVE[chat_id]
        live.close()


async def _turn(message, eng, *, chat_id: str, user: Optional[str], author: str, store,
                panel: Optional[PanelHook], document: Optional[DocumentHook]) -> None:
    import chainlit as cl

    ws = Workspace(chat_id)
    uploads = []
    for element in message.elements or []:
        if getattr(element, "path", None):
            uploads.append(await cl.make_async(ws.add_upload)(Path(element.path), element.name))
    history = await cl.make_async(load_history)(store, chat_id)
    turn = Turn(chat_id=chat_id, prompt=chat_prompt(message.content or "", uploads), user=user, history=history)
    ui = ChainlitTurn(author, lambda messages: save_history(store, chat_id, messages))
    try:
        result = await eng.run_turn(turn, ui)
    except EngineBusy:
        await ui.finish_steps("—")
        await cl.Message(content="⏳ Jeszcze pracuję nad poprzednim zadaniem w tej rozmowie.", author=author).send()
        return
    if result.messages is not None:
        await cl.make_async(save_history)(store, chat_id, result.messages)
    await ui.finish_steps(_summary(result))
    sent = await ui.deliver(result)
    if panel is not None:
        try:  # the answer matters more than the panel
            turn = await cl.make_async(panel_turn)(chat_id, message.content or "", result)
            if turn is not None:
                await panel(turn, sent)
        except Exception as exc:
            print(f"⚠️ [PANEL] {type(exc).__name__}: {exc}")
    for draft in result.drafts if document is not None else []:
        await document(draft["id"])


def on_stop(chat_id: str) -> None:
    engine().abort(chat_id)


def _mirror(live: LiveTurn) -> None:
    """Mirror this handler's Chainlit emitter (the turn's sends, until Chainlit's task_end after the handler)."""
    from chainlit.context import context_var

    try:
        context = context_var.get()
    except LookupError:
        return
    context.emitter = MirrorEmitter(context.emitter, live)


def _keep_running(chat_id: str) -> None:
    """A message written while the chat's turn runs got the "still working" note: Chainlit's task_end after this
    handler would hide the progress indicator and ⏹ of a turn that is still running, so it is skipped then."""
    from chainlit.context import context_var

    try:
        context = context_var.get()
    except LookupError:
        return
    live = _LIVE.get(chat_id)
    if live is None or not any(getattr(w.emitter, "session", None) is context.session for w in live.watchers):
        return  # this session does not get the turn's task_end: let Chainlit end its indicator now
    context.emitter = KeepRunning(context.emitter, lambda: _LIVE.get(chat_id) is live)


async def rejoin(chat_id: str, author: str) -> bool:
    """on_chat_resume of a chat whose turn is still running (the user came back from another chat): this session
    gets the turn too — the progress indicator and ⏹, a note, the steps, the streamed answer, the agent's questions."""
    live = _LIVE.get(chat_id)
    if live is None or live.closed:
        return False
    import chainlit as cl
    from chainlit.context import context_var
    from chainlit.utils import utc_now

    context = context_var.get()
    watcher = live.add(context.emitter)
    note = cl.Message(content=REJOIN_NOTE, author=author)
    note.created_at = utc_now()
    note_dict = note.to_dict()
    context.emitter = ResumeHook(context.emitter, lambda: live.start(watcher, note_dict))
    asyncio.get_running_loop().call_later(REJOIN_FALLBACK_S, live.start, watcher, note_dict)
    return True


WELCOME = (
    "👋 **Cześć! Jestem {name} — Twój lokalny asystent AI.**\n\n"
    "Pracuję bezpośrednio na dokumentach firmy: wyszukuję je, czytam całe pliki i arkusze, liczę w Pythonie, "
    "przygotowuję pisma w PDF i pliki do pobrania. Gdy czegoś nie wiem, zapytam. Każdy krok widzisz w "
    "„kroki zadania”; przycisk ⏹ zatrzymuje pracę.\n\n"
    "Wszystko działa na tym serwerze: dokumenty i rozmowy nie wychodzą poza firmę."
)


def welcome(name: str) -> str:
    return WELCOME.format(name=name)
