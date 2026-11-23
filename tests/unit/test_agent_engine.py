"""The v3 engine behind the chat UI (src/agent_engine.py): the GPU queue, one runner per turn with its events, the
agent's questions and approvals routed to the UI, abort, timeout, the chat's messages and the result files. The
container is replaced by a small Python process that speaks the runner's protocol (stdout events, the bridge socket,
result and messages files); the real image is checked on the GPU host."""

import asyncio
import dataclasses
import json
import sys
import textwrap

import pytest

from src import agent_engine
from src.agent_engine import AgentEngine, EngineBusy, GpuQueue, Turn, chat_prompt, polish_date
from src.config import get_settings, load_settings

FAKE_RUNNER = textwrap.dedent('''
    import json, signal, socket, sys, time
    from pathlib import Path
    run, sock, results, scenario = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), sys.argv[4]
    spec = json.loads((run / "spec.json").read_text())
    history = json.loads((run / "messages_in.json").read_text())

    def emit(**event):
        print(json.dumps(event), flush=True)

    def bridge(method, params):
        s = socket.socket(socket.AF_UNIX)
        s.connect(sock)
        s.sendall((json.dumps({"method": method, "params": params}) + "\\n").encode())
        data = b""
        while chunk := s.recv(65536):
            data += chunk
        return json.loads(data)

    def finish(status, text):
        messages = history + [{"role": "user", "content": spec["prompt"]}, {"role": "assistant", "content": text}]
        (run / "messages_out.json").write_text(json.dumps(messages))
        (run / "result.json").write_text(json.dumps({"status": status, "text": text, "error": None}))
        emit(ev="result", status=status, text=text)

    signal.signal(signal.SIGTERM, lambda *_: (finish("aborted", "częściowo"), sys.exit(1)))
    emit(ev="text_delta", text="Sprawdzam ")
    emit(ev="text_delta", text="pliki.")
    emit(ev="tool_start", tool="read_files", id="c1", input=json.dumps({"files": [{"path": "/praca/tekst/a.txt"}]}))
    print("not json")
    emit(ev="tool_end", tool="read_files", id="c1", output="treść", error=None, ms=5)
    final = "Gotowe."
    if scenario == "ask":
        reply = bridge("ask", {"question": "Który rok?", "options": ["2025", "2026"]})
        final = "Rok " + reply["result"]["answer"]
    elif scenario == "approve":
        reply = bridge("approve", {"tool": "email_draft", "input": {"to": "a@example.com", "subject": "s", "body": "b"}})
        final = "zgoda" if reply["result"]["approved"] else "odmowa: " + reply["result"]["reason"]
    elif scenario == "sleep":
        time.sleep(30)
    results.mkdir(parents=True, exist_ok=True)
    (results / "raport.txt").write_text("wynik")
    emit(ev="text_delta", text=final)
    finish("ok", final)
''')


class RecordingUI:
    def __init__(self, answer="2026", approve=(True, "")):
        self.events, self.positions, self.asked, self.approvals, self.aborted_with = [], [], [], [], None
        self.answer, self.approve_answer = answer, approve

    async def queued(self, position):
        self.positions.append(position)

    async def event(self, event):
        self.events.append(event)

    async def ask(self, question, options):
        self.asked.append((question, options))
        return self.answer

    async def approve(self, tool, args):
        self.approvals.append((tool, args))
        return self.approve_answer

    async def aborted(self, result):
        self.aborted_with = result


@pytest.fixture
def make_engine(tmp_path, monkeypatch):
    runner = tmp_path / "fake_runner.py"
    runner.write_text(FAKE_RUNNER, encoding="utf-8")
    monkeypatch.setattr(agent_engine, "STOP_WAIT_S", 5)

    def build(scenario="plain", **overrides):
        settings = dataclasses.replace(get_settings(), **overrides) if overrides else get_settings()

        def command(ws, run, dom, sock_dir, name):
            return [sys.executable, str(runner), str(run), str(sock_dir / agent_engine.SOCKET), str(ws.results_dir),
                    scenario]

        return AgentEngine(settings, command=command, workspace_root=tmp_path / "ws")

    return build


def test_a_turn_streams_events_and_returns_answer_messages_and_files(make_engine):
    engine, ui = make_engine(), RecordingUI()
    history = [{"role": "user", "content": "wcześniej"}]
    result = asyncio.run(engine.run_turn(Turn("czat-1", "Podsumuj", history=history), ui))
    assert result.status == "ok" and result.text == "Gotowe."
    assert [e["ev"] for e in ui.events] == ["text_delta", "text_delta", "tool_start", "tool_end", "text_delta", "result"]
    assert ui.positions == [0]
    assert result.tool_calls == {"read_files": 1}
    assert [m["content"] for m in result.messages] == ["wcześniej", "Podsumuj", "Gotowe."]
    assert [f["name"] for f in result.files] == ["raport.txt"]
    run = result.run_dir
    spec = json.loads((run / "spec.json").read_text())
    assert spec["mode"] == "act" and spec["events"] == "stdout"
    assert json.loads((run / "messages_in.json").read_text()) == history
    # the next turn of the same chat starts from the saved messages; the file it wrote again counts as changed
    again = asyncio.run(engine.run_turn(Turn("czat-1", "Jeszcze raz", history=result.messages), RecordingUI()))
    assert [f["name"] for f in again.files] == ["raport.txt"] and len(again.messages) == 5
    assert len(list((run.parent).iterdir())) == 2  # one run folder per turn


def test_questions_and_approvals_reach_the_ui(make_engine):
    ui = RecordingUI(answer="2026")
    result = asyncio.run(make_engine("ask").run_turn(Turn("czat-2", "?"), ui))
    assert ui.asked == [("Który rok?", ["2025", "2026"])] and result.text == "Rok 2026"
    yes = RecordingUI()
    assert asyncio.run(make_engine("approve").run_turn(Turn("czat-3", "?"), yes)).text == "zgoda"
    assert yes.approvals[0][0] == "email_draft"
    no = RecordingUI(approve=(False, "użytkownik nie wyraził zgody"))
    assert asyncio.run(make_engine("approve").run_turn(Turn("czat-4", "?"), no)).text == \
        "odmowa: użytkownik nie wyraził zgody"


def test_a_failing_ui_refuses_instead_of_hanging(make_engine):
    class Broken(RecordingUI):
        async def approve(self, tool, args):
            raise RuntimeError("websocket closed")

    result = asyncio.run(make_engine("approve").run_turn(Turn("czat-5", "?"), Broken()))
    assert result.text.startswith("odmowa: brak odpowiedzi")


def test_stop_aborts_the_runner_and_keeps_the_conversation(make_engine):
    engine, ui = make_engine("sleep"), RecordingUI()

    async def scenario():
        task = asyncio.create_task(engine.run_turn(Turn("czat-6", "długie"), ui))
        while not any(e["ev"] == "tool_end" for e in ui.events):
            await asyncio.sleep(0.05)
        assert engine.busy("czat-6")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert ui.aborted_with is not None and ui.aborted_with.status == "aborted"
    assert ui.aborted_with.messages[-1]["content"] == "częściowo"
    assert not engine.busy("czat-6") and engine.queue.active == set()


def test_a_turn_over_time_is_stopped(make_engine, monkeypatch):
    monkeypatch.setattr(agent_engine, "GRACE_S", 0)
    result = asyncio.run(make_engine("sleep", agent_timeout=1).run_turn(Turn("czat-7", "x"), RecordingUI()))
    assert result.status == "timeout" and "przekroczony czas" in result.error


def test_one_turn_per_chat_and_a_queue_across_chats(make_engine):
    engine = make_engine("plain")
    first, second, same = RecordingUI(), RecordingUI(), RecordingUI()

    async def scenario():
        a = asyncio.create_task(engine.run_turn(Turn("czat-a", "1"), first))
        await asyncio.sleep(0)
        b = asyncio.create_task(engine.run_turn(Turn("czat-b", "2"), second))
        await asyncio.sleep(0)
        with pytest.raises(EngineBusy):
            await engine.run_turn(Turn("czat-a", "znowu"), same)
        return await a, await b

    ra, rb = asyncio.run(scenario())
    assert ra.status == rb.status == "ok"
    assert first.positions == [0] and second.positions == [1, 0]


def test_queue_order_and_leaving_it():
    async def scenario():
        q, heard = GpuQueue(1), {"b": [], "c": []}
        await q.acquire("a", lambda p: asyncio.sleep(0))

        async def wait(name):
            async def tell(p):
                heard[name].append(p)
            await q.acquire(name, tell)
            return name

        b = asyncio.create_task(wait("b"))
        c = asyncio.create_task(wait("c"))
        for _ in range(5):
            await asyncio.sleep(0)
        assert q.waiting == ["b", "c"] and q.position("c") == 2
        b.cancel()  # b leaves the queue; c moves up
        with pytest.raises(asyncio.CancelledError):
            await b
        for _ in range(5):
            await asyncio.sleep(0)
        assert heard["c"] == [2, 1] and q.waiting == ["c"]
        await q.release("a")
        assert await c == "c" and q.active == {"c"}

    asyncio.run(scenario())


def test_spec_and_container_command(tmp_path):
    settings = dataclasses.replace(get_settings(), agent_email=True)
    engine = AgentEngine(settings, workspace_root=tmp_path / "ws")
    spec = engine.spec(Turn("c", "pytanie"))
    assert spec["mode"] == "act" and spec["ask"]["bridge"] == spec["approval"]["bridge"] == "/hapilot/most.sock"
    assert spec["approval"]["ask"] == ["email_draft"] and spec["rules"][0]["name"] == "hapilot"
    assert spec["baseUrl"] == "http://model:8000/v1" and spec["initialMessagesPath"] == "/run/messages_in.json"
    from src.agent_workspace import Workspace

    ws = Workspace("c", root=tmp_path / "ws").prepare()
    cmd = engine.container_command(ws, tmp_path / "run", tmp_path / "dom", tmp_path / "s", "n")
    joined = " ".join(cmd)
    for flag in ("--network hapilot-v3-izolowana", "--read-only", "--cap-drop ALL", "target=/hapilot,readonly",
                 "target=/run", "target=/dom", "target=/praca/dokumenty,readonly", "HAPILOT_EGRESS_ALLOW=model:8000",
                 "HAPILOT_TOOLS=semantic_search,pdf_document,working_days,email_draft", "hapilot-agent:0.9"):
        assert flag in joined
    assert cmd[cmd.index("hapilot-agent:0.9") - 2:cmd.index("hapilot-agent:0.9")] == ["--workdir", "/praca"]


def test_chat_prompt_has_today_and_uploads():
    from datetime import date

    text = chat_prompt("Streść załącznik", [{"path": "/praca/zalaczniki/a.pdf", "text": ["/praca/zalaczniki/a.pdf.txt"],
                                             "error": None},
                                            {"path": "/praca/zalaczniki/b.bin", "text": [], "error": "brak kopii"}],
                       today=date(2026, 11, 19))
    assert text.startswith("Dziś jest czwartek, 19 listopada 2026 r.")
    assert "/praca/zalaczniki/a.pdf (tekst: /praca/zalaczniki/a.pdf.txt)" in text and "b.bin — brak kopii" in text
    assert polish_date(date(2026, 12, 24)) == "czwartek, 24 grudnia 2026 r."


def test_engine_settings():
    s = load_settings({})
    assert s.engine == "v2" and s.agent_model_url == "http://model:8000/v1" and not s.agent_email
    assert load_settings({"HAPILOT_ENGINE": "v3", "HAPILOT_AGENT_EMAIL": "on"}).agent_email
    from src.config import ConfigError, NotLocalEndpoint

    with pytest.raises(ConfigError):
        load_settings({"HAPILOT_ENGINE": "v4"})
    with pytest.raises(NotLocalEndpoint):
        load_settings({"HAPILOT_AGENT_MODEL_URL": "https://api.example.com/v1"})
    assert load_settings({"HAPILOT_AGENT_MODEL_URL": "http://127.0.0.1:8000/v1"}).agent_model_url.startswith("http://127")
