#!/usr/bin/env python3
"""Hapilot v3, stage 4: dry run of the UI's engine (src/agent_engine.py) through real agent containers, without the UI.

A console stand-in for the chat answers the agent's questions and approvals and records every event, so the run
shows what the UI would get. Four scenarios, in this order (the fake model's script, skrypt_fake_s4.json, follows it):

  A  one turn with a working note, a question, search, the PDF catalogue, a file for the user, an e-mail draft that
     needs an approval (with --email), and a streamed answer
  B  the same chat again: the earlier messages go in, the answer comes back
  C  two chats at once: the second waits in the queue (place 1) and runs after the first
  D  a turn stopped while a command runs (the UI's stop button): the runner aborts and the conversation is kept

Run with Hapilot's Python environment and the test settings (HAPILOT_DATA_DIR of a knowledge base copy, documents),
with the fake model and the relay up (window.sh fake). Writes <out>/wynik.json and <out>/<scenario>.events.jsonl.

  engine_run.py --out DIR [--email --fake-n8n] [--answer TEXT] [--approve yes|no] [--image hapilot-agent:0.7]
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))


class ConsoleUI:
    def __init__(self, name: str, out: Path, answer: str, approve: bool):
        self.name, self.out, self.answer, self.approve_answer = name, out, answer, approve
        self.events, self.positions, self.asked, self.approvals, self.aborted_result = [], [], [], [], None

    def _log(self, entry: dict) -> None:
        entry = {"t": round(time.monotonic(), 2), **entry}
        with open(self.out / f"{self.name}.events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    async def queued(self, position):
        self.positions.append(position)
        self._log({"ev": "queued", "position": position})

    async def event(self, event):
        self.events.append(event)
        self._log(event)

    async def ask(self, question, options):
        self.asked.append({"question": question, "options": options})
        self._log({"ev": "asked", "question": question, "options": options, "answer": self.answer})
        return self.answer

    async def approve(self, tool, args):
        self.approvals.append(tool)
        self._log({"ev": "approval", "tool": tool, "fields": sorted(args), "approved": self.approve_answer})
        return (True, "") if self.approve_answer else (False, "użytkownik nie wyraził zgody")

    async def aborted(self, result):
        self.aborted_result = result
        self._log({"ev": "aborted", "status": result.status, "messages": len(result.messages or [])})


def summary(ui: ConsoleUI, result) -> dict:
    kinds = [e.get("ev") for e in ui.events]
    return {"status": result.status if result else None, "text": (result.text if result else "")[:300],
            "error": result.error if result else None, "positions": ui.positions,
            "text_deltas": kinds.count("text_delta"), "tools": result.tool_calls if result else {},
            "asked": ui.asked, "approvals": ui.approvals,
            "messages": len(result.messages or []) if result else None,
            "files": [f["name"] for f in result.files] if result else [],
            "run_dir": str(result.run_dir) if result else None, "seconds": result.duration_s if result else None}


async def scenarios(a, engine_mod, settings) -> dict:
    from src.agent_engine import AgentEngine, Turn, chat_prompt

    engine = AgentEngine(settings)
    out, report = Path(a.out), {}
    mk = lambda name: ConsoleUI(name, out, a.answer, a.approve == "yes")  # noqa: E731

    ui = mk("A")
    ra = await engine.run_turn(Turn("suchy-a", chat_prompt("Przygotuj notatkę o terminie reklamacji i szkic e-maila.")),
                               ui)
    report["A"] = summary(ui, ra)

    ui = mk("B")
    rb = await engine.run_turn(Turn("suchy-a", chat_prompt("Dziękuję, a krócej?"), history=ra.messages or []), ui)
    report["B"] = summary(ui, rb) | {"history_in": len(ra.messages or [])}

    ub, uc = mk("C1"), mk("C2")
    t1 = asyncio.create_task(engine.run_turn(Turn("suchy-b", chat_prompt("Pierwszy użytkownik")), ub))
    await asyncio.sleep(0.2)
    t2 = asyncio.create_task(engine.run_turn(Turn("suchy-c", chat_prompt("Drugi użytkownik")), uc))
    r1, r2 = await t1, await t2
    report["C"] = {"first": summary(ub, r1), "second": summary(uc, r2)}

    ud = mk("D")
    td = asyncio.create_task(engine.run_turn(Turn("suchy-d", chat_prompt("Policz coś długiego.")), ud))
    while not any(e.get("ev") == "tool_start" for e in ud.events):
        await asyncio.sleep(0.2)
    await asyncio.sleep(3)
    stopped_at = time.monotonic()
    td.cancel()
    try:
        await td
    except asyncio.CancelledError:
        pass
    report["D"] = summary(ud, ud.aborted_result) | {"stop_seconds": round(time.monotonic() - stopped_at, 1)}
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--image", default="hapilot-agent:0.7")
    ap.add_argument("--answer", default="Tak, proszę")
    ap.add_argument("--approve", choices=["yes", "no"], default="yes")
    ap.add_argument("--email", action="store_true", help="enable email_draft (needs n8n or --fake-n8n)")
    ap.add_argument("--fake-n8n", action="store_true", help="a local stand-in for n8n's draft webhook")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.fake_n8n:  # before the settings are read: the bridge takes n8n's address from them
        from tools_run import fake_n8n

        _server, base = fake_n8n(out / "n8n.jsonl")
        os.environ["HAPILOT_N8N_WEBHOOK_BASE"] = base
    from src import agent_engine
    from src.config import get_settings, reset_settings

    reset_settings()
    settings = dataclasses.replace(get_settings(), agent_image=a.image, agent_email=a.email)
    report = asyncio.run(scenarios(a, agent_engine, settings))
    report["settings"] = {"image": settings.agent_image, "model": settings.agent_model, "email": settings.agent_email}
    (out / "wynik.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
