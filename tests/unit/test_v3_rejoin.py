"""Engine v3: a chat opened again while its turn runs gets the turn too (src/ui_agent.py: LiveTurn, MirrorEmitter).

Found on the pilot (2026-11-23): the user wrote a message, went to another chat and came back at once; the progress
indicator was gone and the answer never came, though the turn finished and the answer was saved — Chainlit gives the
returning browser a new session and the turn kept sending to the old one."""

import asyncio

from src import ui_agent
from src.ui_agent import KeepRunning, LiveTurn, MirrorEmitter, ResumeHook


class FakeEmitter:
    """Records what a session would get; ``answer`` is what its user answers to a question (None: no answer)."""

    def __init__(self, answer=None, ask_delay=0.0, broken=False):
        self.calls, self.answer, self.ask_delay, self.broken = [], answer, ask_delay, broken
        self.session = object()

    def _record(self, name):
        async def call(*args, **kwargs):
            if self.broken:
                raise ConnectionError("gone")
            self.calls.append((name, args, kwargs))
        return call

    def __getattr__(self, name):
        if name in ui_agent.MIRRORED_CALLS or name in ("clear", "resume_thread"):
            return self._record(name)
        raise AttributeError(name)

    async def emit(self, event, data):
        self.calls.append(("emit", (event, data), {}))

    async def send_ask_user(self, step_dict, spec, raise_on_timeout=False):
        self.calls.append(("ask", (step_dict["id"],), {}))
        await asyncio.sleep(self.ask_delay)
        return self.answer

    def names(self):
        return [c[0] if c[0] != "emit" else f"emit:{c[1][0]}" for c in self.calls]


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


async def _end(live, turn):
    """What on_message and Chainlit do after every turn: close it, then Chainlit's task_end."""
    live.close()
    await turn.task_end()
    await _settle()


def _step(sid, output=""):
    return {"id": sid, "type": "assistant_message", "output": output}


def test_a_watcher_gets_the_turn_after_its_history_and_in_order():
    async def run():
        live, old, new = LiveTurn(), FakeEmitter(), FakeEmitter()
        turn = MirrorEmitter(old, live)
        await turn.send_step(_step("krok-1"))  # before the user came back: in the resumed history already
        watcher = live.add(new)
        await turn.send_step(_step("krok-2"))  # while the history loads: kept until it is on the screen
        await turn.stream_start(_step("odp", "Dzi"))
        await turn.send_token(id="odp", token="ś je", is_sequence=False)
        await _settle()
        assert new.calls == []
        live.start(watcher, _step("notka", "⏳"))
        await turn.send_token("odp", "st")  # positional, as Chainlit's Step does it
        await _settle()
        assert new.names() == ["task_start", "send_step", "send_step", "stream_start", "send_token"]
        assert new.calls[1][1][0]["id"] == "notka" and new.calls[2][1][0]["id"] == "krok-2"
        assert new.calls[3][1][0]["output"] == "Dziś je"  # the stream so far, once
        assert new.calls[4][1] == ("odp", "st")
        assert [c[0] for c in old.calls] == ["send_step", "send_step", "stream_start", "send_token", "send_token"]
        await turn.send_step(_step("odp", "Dziś jest"))  # the stream ends
        assert live.streaming == {}
        live.close()
        await turn.task_end()  # Chainlit's, after the handler
        await _settle()
        assert new.names()[-3:] == ["send_step", "delete_step", "task_end"]
        assert new.calls[-2][1][0]["id"] == "notka"
        assert watcher.pump.done()  # the sender stops after the turn's task_end
    asyncio.run(run())


def test_only_the_panel_and_buttons_events_are_mirrored():
    async def run():
        live, old, new = LiveTurn(), FakeEmitter(), FakeEmitter()
        turn = MirrorEmitter(old, live)
        live.start(live.add(new))
        await turn.emit("action", {"name": "show_panel"})
        await turn.emit("set_sidebar_elements", {"elements": []})
        await turn.emit("first_interaction", {"interaction": "x"})
        await _settle()
        assert new.names() == ["task_start", "emit:action", "emit:set_sidebar_elements"]
        assert len(old.calls) == 3
        await _end(live, turn)
    asyncio.run(run())


def test_a_question_is_answered_in_the_session_where_the_user_is():
    async def run():
        live = LiveTurn()
        old = FakeEmitter(answer=None, ask_delay=0.05)  # the session the user left: no answer, times out
        new = FakeEmitter(answer={"name": "v3_odpowiedz", "payload": {"value": "Tak"}}, ask_delay=0.01)
        turn = MirrorEmitter(old, live)
        live.start(live.add(new))
        answer = await turn.send_ask_user(_step("pytanie"), spec=None)
        assert answer["payload"]["value"] == "Tak"
        assert ("ask", ("pytanie",), {}) in new.calls and ("ask", ("pytanie",), {}) in old.calls
        assert ("clear", ("clear_ask",), {}) in old.calls  # the other screen's question is taken away
        assert live.ask is None and live.ask_tasks == []
        await _end(live, turn)
    asyncio.run(run())


def test_a_session_that_comes_while_the_question_waits_gets_it():
    async def run():
        live = LiveTurn()
        old = FakeEmitter(answer=None, ask_delay=0.2)
        late = FakeEmitter(answer={"output": "Wrocław"}, ask_delay=0.01)
        turn = MirrorEmitter(old, live)
        asking = asyncio.ensure_future(turn.send_ask_user(_step("pytanie"), spec=None))
        await _settle()
        watcher = live.add(late)
        live.start(watcher)
        assert (await asking) == {"output": "Wrocław"}
        assert ("ask", ("pytanie",), {}) in late.calls
        await _end(live, turn)
    asyncio.run(run())


def test_no_answer_anywhere_is_none_as_before():
    async def run():
        live = LiveTurn()
        turn = MirrorEmitter(FakeEmitter(answer=None), live)
        live.start(live.add(FakeEmitter(answer=None)))
        assert await turn.send_ask_user(_step("pytanie"), spec=None) is None
        await _end(live, turn)
    asyncio.run(run())


def test_a_gone_session_does_not_stop_the_turn():
    async def run():
        live, old, gone = LiveTurn(), FakeEmitter(), FakeEmitter(broken=True)
        turn = MirrorEmitter(old, live)
        live.start(live.add(gone))
        await _settle()
        await turn.send_step(_step("krok"))
        await _settle()
        assert live.watchers == [] and old.names() == ["send_step"]
    asyncio.run(run())


def test_a_session_ready_after_the_turn_ended_gets_the_answer_but_no_progress():
    async def run():
        live, old, new = LiveTurn(), FakeEmitter(), FakeEmitter()
        turn = MirrorEmitter(old, live)
        watcher = live.add(new)
        await turn.send_step(_step("odp", "Gotowe."))
        live.close()
        live.start(watcher, _step("notka"))
        await turn.task_end()
        await _settle()
        assert new.names() == ["send_step", "task_end"]
    asyncio.run(run())


def test_resume_hook_starts_the_watcher_after_the_history_and_only_once():
    async def run():
        live, new = LiveTurn(), FakeEmitter()
        watcher = live.add(new)
        hook = ResumeHook(new, lambda: live.start(watcher))
        await hook.resume_thread({"id": "t"})
        live.start(watcher)  # the fallback timer comes later: nothing twice
        await _settle()
        assert new.names() == ["resume_thread", "task_start"]
    asyncio.run(run())


def test_keep_running_skips_task_end_only_while_the_turn_runs():
    async def run():
        inner, running = FakeEmitter(), [True]
        keep = KeepRunning(inner, lambda: running[0])
        await keep.task_end()
        running[0] = False
        await keep.task_end()
        await keep.send_step(_step("x"))
        assert inner.names() == ["task_end", "send_step"]
    asyncio.run(run())
