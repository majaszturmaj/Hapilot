"""The v3 engine behind the chat UI: one agent turn = one fresh agent container (image hapilot-agent).

    UI (src/ui_agent.py) ── run_turn(Turn, ui) ──► GPU queue (one slot by default: one model, one request at a time)
                                                  │
                     host bridge (src/agent_bridge.py, Unix socket) ◄── tools, questions, approvals
                                                  │
                     docker run hapilot-agent … run_task.mjs  (mode "act", events on stdout, the chat's messages)

Per turn: the conversation's workspace (src/agent_workspace.py: documents, text mirror and uploads read-only, files
for the user in /praca/wyniki), a run folder ``<workspace>/przebiegi/<time>-<n>/`` with the spec, the runner's trace,
the bridge's trace, the egress log, the result and the conversation before and after the turn. The agent's events
reach the UI as they happen; its questions (ask_question) and approvals (an e-mail draft) are put to the user in the
UI by the same coroutine that runs the turn, so they appear in the user's own chat. The chat's model-side history
(the SDK's messages) is the caller's to keep: it goes in with the turn and comes back with the result.

Stopping: cancelling ``run_turn`` (the UI's stop button) sends SIGTERM to the runner, which aborts the session and
still writes the conversation so far; after ``STOP_WAIT_S`` the container is killed. A turn waiting in the queue just
leaves it. The whole turn is limited to HAPILOT_AGENT_TIMEOUT plus a grace period.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import itertools
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Awaitable, Callable, Optional, Protocol

from src.agent_bridge import Bridge
from src.agent_workspace import ADDED, DOCS, ROOT, TEXT, UPLOADS, Workspace, folder_name
from src.config import Settings, get_settings

RUNNER = "/opt/hapilot-agent"
BRIDGE_DIR = "/hapilot"
SOCKET = "most.sock"
TOOLS = ("semantic_search", "pdf_document", "working_days")
NEEDS_APPROVAL = ("email_draft",)
# Cline's own tools in act mode (the SDK's "act" preset): read_files, search_codebase, run_commands, editor and
# ask_question; its web fetch and skills are switched off by the runner (run_task.mjs ACT_DISABLED), apply_patch is
# off in that preset, subagents and teams stay off. Shown in the agent panel (src/admin.py v3_data).
CLINE_TOOLS = ("read_files", "search_codebase", "run_commands", "editor", "ask_question")
LIMITS = {"memory": "16g", "cpus": "8", "pids": "1024", "tmp": "2g"}  # per container (one per chat turn)
TOOL_LABELS = {  # what the chat shows while a tool runs (src/ui_agent.py), also the panel's names
    "read_files": "📖 Czytam pliki",
    "search_codebase": "🔎 Szukam w plikach",
    "run_commands": "🐍 Uruchamiam polecenia",
    "editor": "✏️ Zapisuję plik",
    "apply_patch": "✏️ Zmieniam plik",
    "ask_question": "❓ Pytanie do Ciebie",
    "semantic_search": "🧠 Szukam w bazie wiedzy",
    "pdf_document": "📄 Dokument PDF",
    "working_days": "📅 Dni robocze i terminy",
    "email_draft": "📧 Szkic e-maila",
}
GRACE_S = 60
STOP_WAIT_S = 20
QUESTION_TIMEOUT_S = 900
STDOUT_LIMIT = 4 * 1024 * 1024
NO_ANSWER = "(użytkownik nie odpowiedział)"

WEEKDAYS = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
MONTHS = ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia", "września",
          "października", "listopada", "grudnia"]


class EngineBusy(RuntimeError):
    """This chat already has a turn running."""


def polish_date(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]} {d.year} r."


def chat_prompt(text: str, uploads: Optional[list] = None, today: Optional[date] = None) -> str:
    """What the agent gets for a user message: today's date (the rules count from it), the message, the uploads."""
    lines = [f"Dziś jest {polish_date(today or date.today())}.", "", text.strip()]
    if uploads:
        lines += ["", f"Załączniki użytkownika (tylko do odczytu, w {ROOT}/{UPLOADS}):"]
        for u in uploads:
            note = f"- {u['path']}"
            if u.get("text"):
                note += " (tekst: " + ", ".join(u["text"]) + ")"
            if u.get("error"):
                note += f" — {u['error']}"
            lines.append(note)
    return "\n".join(lines)


@dataclass
class Turn:
    chat_id: str
    prompt: str
    user: Optional[str] = None
    history: list = field(default_factory=list)  # the SDK's messages of this chat so far
    signature: str = "blank"


@dataclass
class TurnResult:
    status: str  # ok | error | timeout | aborted
    text: str
    messages: Optional[list]  # the conversation after the turn; None: the runner wrote none (keep the old one)
    files: list  # [{"name", "path", "bytes", "status"}] new or changed in /praca/wyniki (status created | modified)
    error: Optional[str]
    run_dir: Path
    duration_s: float
    tool_calls: dict = field(default_factory=dict)
    retrievals: list = field(default_factory=list)  # semantic_search calls: {"query", "k", "ms", "chunks"}
    opened: list = field(default_factory=list)  # container paths of documents the agent read (side panel)
    drafts: list = field(default_factory=list)  # PDF drafts for the user's form: {"id", "template_id"}


class TurnUI(Protocol):
    async def queued(self, position: int) -> None: ...  # place in the queue; 0 = the turn starts now
    async def event(self, event: dict) -> None: ...  # an agent event from the runner (text, tools, notices)
    async def ask(self, question: str, options: list) -> str: ...  # the agent asks the user
    async def approve(self, tool: str, args: dict) -> tuple[bool, str]: ...  # a tool that leaves the container
    async def aborted(self, result: TurnResult) -> None: ...  # the turn was stopped; the result so far


class GpuQueue:
    """First come, first served; ``slots`` turns at a time. Waiting turns hear their place when it changes."""

    def __init__(self, slots: int = 1):
        self.slots = max(1, slots)
        self.active: set = set()
        self.waiting: list = []
        self._cond: Optional[asyncio.Condition] = None

    def _condition(self) -> asyncio.Condition:
        if self._cond is None:
            self._cond = asyncio.Condition()
        return self._cond

    def position(self, ticket) -> int:
        """1, 2, … while waiting; 0 when running or unknown."""
        return self.waiting.index(ticket) + 1 if ticket in self.waiting else 0

    async def acquire(self, ticket, on_position: Callable[[int], Awaitable[None]]) -> None:
        cond = self._condition()
        async with cond:
            self.waiting.append(ticket)
        told = None
        try:
            while True:
                async with cond:
                    while True:
                        if self.waiting[0] == ticket and len(self.active) < self.slots:
                            self.waiting.pop(0)
                            self.active.add(ticket)
                            cond.notify_all()
                            return
                        place = self.waiting.index(ticket) + 1
                        if place != told:
                            break
                        await cond.wait()
                told = place
                await on_position(place)
        except BaseException:
            async with cond:
                if ticket in self.waiting:
                    self.waiting.remove(ticket)
                    cond.notify_all()
            raise

    async def release(self, ticket) -> None:
        cond = self._condition()
        async with cond:
            self.active.discard(ticket)
            cond.notify_all()


Command = Callable[[Workspace, Path, Path, Path, str], list]


class AgentEngine:
    def __init__(self, settings: Optional[Settings] = None, *, command: Optional[Command] = None, slots: int = 1,
                 workspace_root: Optional[Path] = None, pdf_drafts: bool = False):
        """``pdf_drafts``: the chat's engine — a PDF is a draft the user checks in a form (src/agent_documents.py)."""
        self.settings = settings or get_settings()
        self.pdf_drafts = pdf_drafts
        self.queue = GpuQueue(slots)
        self.command = command or self.container_command
        self.workspace_root = workspace_root
        self._numbers = itertools.count(1)
        self._running: dict[str, tuple[str, asyncio.subprocess.Process]] = {}
        self._chats: set[str] = set()

    # ----------------------------------------------------------------- container

    def container_command(self, ws: Workspace, run: Path, dom: Path, sock_dir: Path, name: str) -> list:
        s = self.settings
        tools = ",".join(TOOLS + (NEEDS_APPROVAL if s.agent_email else ()))
        return ["docker", "run", "--rm", "--name", name, "--network", s.agent_network,
                "--user", f"{os.getuid()}:{os.getgid()}", "--memory", LIMITS["memory"], "--cpus", LIMITS["cpus"],
                "--pids-limit", LIMITS["pids"], "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
                "--tmpfs", f"/tmp:rw,nosuid,size={LIMITS['tmp']}",
                "-e", "HOME=/dom", "-e", "DO_NOT_TRACK=1", "-e", "MPLCONFIGDIR=/tmp/mpl",
                "-e", "HAPILOT_EGRESS_LOG=/run/egress.jsonl", "-e", f"HAPILOT_EGRESS_ALLOW={s.agent_allow}",
                "-e", f"HAPILOT_TOOLS={tools}", "-e", f"HAPILOT_BRIDGE={BRIDGE_DIR}/{SOCKET}",
                *ws.docker_args()[:-2],
                "--mount", f"type=bind,source={sock_dir},target={BRIDGE_DIR},readonly",
                "--mount", f"type=bind,source={run.resolve()},target=/run",
                "--mount", f"type=bind,source={dom.resolve()},target=/dom",
                "--workdir", ROOT, s.agent_image,
                "node", "--import", f"{RUNNER}/egress_guard.mjs", f"{RUNNER}/run_task.mjs", "/run/spec.json"]

    def spec(self, turn: Turn) -> dict:
        s = self.settings
        bridge = f"{BRIDGE_DIR}/{SOCKET}"
        spec = {"prompt": turn.prompt, "cwd": ROOT, "providerId": "openai-compatible", "modelId": s.agent_model,
                "apiKey": "lokalny", "baseUrl": s.agent_model_url, "timeoutSeconds": s.agent_timeout,
                "tracePath": "/run/trace.jsonl", "resultPath": "/run/result.json", "mode": "act", "events": "stdout",
                "initialMessagesPath": "/run/messages_in.json", "messagesPath": "/run/messages_out.json",
                "mcp": {"command": "python3", "args": [f"{RUNNER}/mcp/hapilot_mcp.py"], "timeoutSeconds": 300},
                "approval": {"ask": list(NEEDS_APPROVAL), "bridge": bridge}, "ask": {"bridge": bridge}}
        rules = Path(s.agent_rules) if s.agent_rules else None
        if rules and rules.is_file():
            spec["rules"] = [{"name": "hapilot", "instructions": rules.read_text(encoding="utf-8")}]
        return spec

    # ----------------------------------------------------------------- turns

    def busy(self, chat_id: str) -> bool:
        return chat_id in self._chats

    async def run_turn(self, turn: Turn, ui: TurnUI) -> TurnResult:
        if turn.chat_id in self._chats:
            raise EngineBusy(turn.chat_id)
        self._chats.add(turn.chat_id)
        n = next(self._numbers)
        ticket = (turn.chat_id, n)
        try:
            await self.queue.acquire(ticket, ui.queued)
            try:
                await ui.queued(0)
                return await self._run(turn, ui, n)
            finally:
                await self.queue.release(ticket)
        finally:
            self._chats.discard(turn.chat_id)

    def abort(self, chat_id: str) -> bool:
        """Ask the running turn of a chat to stop (SIGTERM to the runner); True when one was running."""
        running = self._running.get(chat_id)
        if running is None:
            return False
        with contextlib.suppress(ProcessLookupError):
            running[1].send_signal(signal.SIGTERM)
        return True

    async def _run(self, turn: Turn, ui: TurnUI, n: int) -> TurnResult:
        loop = asyncio.get_running_loop()
        ws = Workspace(turn.chat_id, root=self.workspace_root).prepare()
        run = ws.dir / "przebiegi" / f"{time.strftime('%Y%m%d-%H%M%S')}-{n}"
        dom = ws.dir / "dom"
        run.mkdir(parents=True)
        dom.mkdir(exist_ok=True)
        (run / "messages_in.json").write_text(json.dumps(turn.history or [], ensure_ascii=False), encoding="utf-8")
        (run / "spec.json").write_text(json.dumps(self.spec(turn), ensure_ascii=False, indent=2), encoding="utf-8")
        before = _snapshot(ws)
        inbox: asyncio.Queue = asyncio.Queue()
        pending: set[concurrent.futures.Future] = set()

        def from_bridge(kind: str, *args):
            """Called in a bridge thread: hand the request to the turn's coroutine and wait for the user."""
            future: concurrent.futures.Future = concurrent.futures.Future()
            pending.add(future)
            loop.call_soon_threadsafe(inbox.put_nowait, ("ui", kind, args, future))
            try:
                return future.result(timeout=QUESTION_TIMEOUT_S)
            except (concurrent.futures.TimeoutError, concurrent.futures.CancelledError):
                return None
            finally:
                pending.discard(future)

        def approver(tool: str, args: dict) -> tuple[bool, str]:
            answer = from_bridge("approve", tool, args)
            return tuple(answer) if answer else (False, "brak odpowiedzi użytkownika")

        def asker(question: str, options: list) -> str:
            return from_bridge("ask", question, options) or NO_ANSWER

        bridge = Bridge(ws, user=turn.user, signature=turn.signature, email=self.settings.agent_email,
                        approver=approver, asker=asker, trace_path=run / "bridge.jsonl", record_chunks=True,
                        pdf_drafts=self.pdf_drafts)
        sock_dir = Path(tempfile.mkdtemp(prefix="hb-"))
        name = f"hapilot-v3-czat-{folder_name(turn.chat_id)[:24]}-{n}"
        started = time.monotonic()
        tools: dict[str, int] = {}
        opened: list[str] = []
        status, error = None, None
        proc = None
        try:
            with bridge.serve(sock_dir / SOCKET):
                proc = await asyncio.create_subprocess_exec(
                    *self.command(ws, run, dom, sock_dir, name), stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=STDOUT_LIMIT)
                self._running[turn.chat_id] = (name, proc)
                reader = asyncio.create_task(_read_events(proc, inbox))
                stderr = asyncio.create_task(proc.stderr.read())
                deadline = loop.time() + self.settings.agent_timeout + GRACE_S
                try:
                    while True:
                        left = deadline - loop.time()
                        if left <= 0:
                            raise asyncio.TimeoutError
                        item = await asyncio.wait_for(inbox.get(), timeout=left)
                        if item[0] == "eof":
                            break
                        if item[0] == "event":
                            event = item[1]
                            if event.get("ev") == "tool_end":
                                tools[event.get("tool")] = tools.get(event.get("tool"), 0) + 1
                            elif event.get("ev") == "tool_start":
                                opened += [p for p in opened_paths(event.get("tool"), event.get("input"))
                                           if p not in opened]
                            await _safely(ui.event(event))
                        elif item[0] == "ui":
                            _kind, kind, args, future = item
                            if future.done():
                                continue
                            try:
                                answer = await (ui.ask(*args) if kind == "ask" else ui.approve(*args))
                            except Exception as exc:  # a UI failure is a refusal, never a hang
                                print(f"⚠️ [AGENT] {kind} failed: {type(exc).__name__}: {exc}")
                                answer = None
                            if not future.done():
                                future.set_result(answer)
                    await asyncio.wait_for(proc.wait(), timeout=STOP_WAIT_S)
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(asyncio.shield(stderr), timeout=5)
                except asyncio.TimeoutError:
                    status, error = "timeout", f"przekroczony czas ({self.settings.agent_timeout} s)"
                    await self._stop(name, proc)
                except asyncio.CancelledError:
                    await asyncio.shield(self._stop(name, proc))
                    result = self._result(ws, run, before, "aborted", "przerwane przez użytkownika", started, tools,
                                          bridge.retrievals, opened, bridge.drafts)
                    await _safely(ui.aborted(result))
                    raise
                finally:
                    for future in list(pending):
                        future.cancel()
                    reader.cancel()
                    if not stderr.done():
                        stderr.cancel()
                    else:
                        (run / "stderr.txt").write_bytes(stderr.result() or b"")
        finally:
            self._running.pop(turn.chat_id, None)
            shutil.rmtree(sock_dir, ignore_errors=True)
        return self._result(ws, run, before, status, error, started, tools, bridge.retrievals, opened, bridge.drafts)

    async def _stop(self, name: str, proc) -> None:
        """SIGTERM (the runner aborts and saves the conversation), then a hard kill of the container."""
        if proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(signal.SIGTERM)  # docker run passes it on to the runner
        try:
            await asyncio.wait_for(proc.wait(), timeout=STOP_WAIT_S)
            return
        except asyncio.TimeoutError:
            pass
        if self.command == self.container_command:
            await asyncio.to_thread(subprocess.run, ["docker", "kill", name], capture_output=True)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=STOP_WAIT_S)

    def _result(self, ws: Workspace, run: Path, before: dict, status: Optional[str], error: Optional[str],
                started: float, tools: dict, retrievals: Optional[list] = None,
                opened: Optional[list] = None, drafts: Optional[list] = None) -> TurnResult:
        data = _read_json(run / "result.json") or {}
        messages = _read_json(run / "messages_out.json")
        if status is None:
            status = data.get("status") or "error"
            if status != "ok" and not error:
                error = (data.get("error") or "")[:500] or ("runner wrote no result" if not data else None)
        return TurnResult(status=status, text=str(data.get("text") or ""),
                          messages=messages if isinstance(messages, list) else None,
                          files=_changed(ws, before), error=error, run_dir=run,
                          duration_s=round(time.monotonic() - started, 2), tool_calls=tools,
                          retrievals=list(retrievals or []), opened=list(opened or []), drafts=list(drafts or []))


# --------------------------------------------------------------------------- helpers

async def _read_events(proc, inbox: asyncio.Queue) -> None:
    """The runner's stdout: one JSON object per line (anything else is skipped), then an end marker."""
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                inbox.put_nowait(("event", event))
    except ValueError:  # a line over the limit: stop reading events, the result file still counts
        pass
    finally:
        inbox.put_nowait(("eof",))


async def _safely(awaitable) -> None:
    try:
        await awaitable
    except Exception as exc:  # the UI must not break the turn
        print(f"⚠️ [AGENT] UI callback failed: {type(exc).__name__}: {exc}")


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _snapshot(ws: Workspace) -> dict:
    return {f["name"]: (f["bytes"], f["path"].stat().st_mtime_ns) for f in ws.results()}


def _changed(ws: Workspace, before: dict) -> list:
    out = []
    for f in ws.results():
        if before.get(f["name"]) != (f["bytes"], f["path"].stat().st_mtime_ns):
            out.append({**f, "status": "modified" if f["name"] in before else "created"})
    return out


DOCUMENT_ROOTS = tuple(f"{ROOT}/{d}/" for d in (DOCS, ADDED, TEXT, UPLOADS))
_QUOTED_PATH = re.compile(r"""(['"])(/praca/[^'"\n]+)\1""")
_BARE_PATH = re.compile(r"""/praca/[^\s'"`;|&<>(){}\[\],]+""")


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def opened_paths(tool: Optional[str], raw) -> list[str]:
    """Container paths of the documents a tool call opens, for the side panel: the files ``read_files`` reads and the
    document paths a command names (cat, sed, pandas …). Only paths under the documents, the text mirror and the
    uploads; folders and globs are dropped later, when the paths are matched to documents."""
    if tool not in ("read_files", "run_commands"):
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) and raw else raw
    except ValueError:
        data = raw
    found: list[str] = []
    for text in _strings(data):
        if tool == "read_files":
            if text.startswith(DOCUMENT_ROOTS):
                found.append(text)
            continue
        rest = text
        for match in _QUOTED_PATH.finditer(text):
            found.append(match.group(2))
            rest = rest.replace(match.group(0), " ")
        found += _BARE_PATH.findall(rest)
    paths = (p.rstrip(".:") for p in found)
    return [p for p in dict.fromkeys(paths) if p.startswith(DOCUMENT_ROOTS) and not p.endswith("/")]


_ENGINE: Optional[AgentEngine] = None


def engine() -> AgentEngine:
    """The UI process's engine (one GPU queue for all chats)."""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = AgentEngine(pdf_drafts=True)
    return _ENGINE
