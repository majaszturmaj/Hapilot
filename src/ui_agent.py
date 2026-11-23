"""Chat with the v3 engine (HAPILOT_ENGINE=v3): app.py hands each user message here.

What the user sees for one message:
  ⏳ a note while the request waits for the model (other users' requests run first), updated with its place in line
  "kroki zadania"   one step per tool call of the agent (reading files, commands, search, PDF, working days, …),
                    with its input and a short piece of its output; text the agent writes before a tool call becomes a
                    "💬" step too, so only the final answer stays a message
  the answer        streamed as the model writes it; files the agent left in /praca/wyniki are attached below it
  📎 the side panel  "Źródła i pliki" as in engine v2 (src/ui_panel.py): the documents the answer names, the agent read
                    or its searches returned, shown whole (PDF pages, search fragments highlighted), and the new files
                    (a PDF as pages); it opens after the answer, and the button under the answer opens it again
  ❓ / 📧            the agent's questions (buttons for its options, or free text) and approvals (an e-mail draft:
                    the draft's recipients, subject and text, ✅ / ❌), in the user's own chat
  ⏹                 Chainlit's stop button aborts the turn; the conversation so far is kept

The model-side history of a chat (the SDK's messages) is saved with the chat (agent_state ``v3_messages``), so a
resumed chat continues where it was; the chat's workspace (src/agent_workspace.py) keeps its files.

Chainlit is imported inside the functions: the helpers above the UI code are plain and unit-tested without it.
"""

from __future__ import annotations

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


def file_list(files: list) -> str:
    if not files:
        return ""
    return "\n\n⬇️ **" + ("Plik do pobrania poniżej." if len(files) == 1 else "Pliki do pobrania poniżej.") + "**"


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
            kind = cl.Image if Path(f["name"]).suffix.lower() in IMAGE_TYPES else cl.File
            elements.append(kind(name=Path(f["name"]).name, path=str(f["path"]), display="inline"))
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


def panel_turn(chat_id: str, question: str, result: TurnResult) -> Optional[dict]:
    """The side panel's data for a finished v3 turn (None: no documents, no files)."""
    from src import ui_panel

    return ui_panel.build_agent_turn(chat_id, question, answer_text(result, ""), result.files, result.retrievals,
                                     result.opened, Workspace(chat_id).uploads)


async def on_message(message, *, chat_id: str, user: Optional[str], author: str, store,
                     panel: Optional[PanelHook] = None) -> None:
    """``panel(turn, answer_message)`` keeps and shows the side panel's turn (src/app.py)."""
    import chainlit as cl

    eng = engine()
    if eng.busy(chat_id):
        await cl.Message(content="⏳ Jeszcze pracuję nad poprzednim zadaniem w tej rozmowie. Zatrzymaj je (⏹) albo "
                                 "poczekaj na odpowiedź.", author=author).send()
        return
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
    if panel is None:
        return
    try:  # the answer matters more than the panel
        turn = await cl.make_async(panel_turn)(chat_id, message.content or "", result)
        if turn is not None:
            await panel(turn, sent)
    except Exception as exc:
        print(f"⚠️ [PANEL] {type(exc).__name__}: {exc}")


def on_stop(chat_id: str) -> None:
    engine().abort(chat_id)


WELCOME = (
    "👋 **Cześć! Jestem {name} — Twój lokalny asystent AI.**\n\n"
    "Pracuję bezpośrednio na dokumentach firmy: wyszukuję je, czytam całe pliki i arkusze, liczę w Pythonie, "
    "przygotowuję pisma w PDF i pliki do pobrania. Gdy czegoś nie wiem, zapytam. Każdy krok widzisz w "
    "„kroki zadania”; przycisk ⏹ zatrzymuje pracę.\n\n"
    "Wszystko działa na tym serwerze: dokumenty i rozmowy nie wychodzą poza firmę."
)


def welcome(name: str) -> str:
    return WELCOME.format(name=name)
