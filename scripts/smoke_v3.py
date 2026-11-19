#!/usr/bin/env python3
"""Smoke scenarios through the v3 chat engine (src/agent_engine.py) with a real model, on the GPU host:

    .venv/bin/python scripts/smoke_v3.py [--profile best-local] [--egress-guard] [--report FILE]

The same scenarios as v2's smoke (tests/scenarios/smoke.yaml; `engines: [v2]` ones are skipped), the same checks on
the answer and the files the turn produced, in the chat configuration of the UI: Cline's act mode, one container per
turn, the chat's messages carried from turn to turn. Nobody answers: a question gets its first option, an approval is
refused. The knowledge base is a temporary one built from the demo company (HAPILOT_DOCS_DIR), as in v2's smoke.

The model server and the relay of the agent network must be up (the operator's; config/agent_profiles.yaml names the
model). The report has, besides the host's egress guard, every connection the agent containers' runners refused.
Exit code 0 only if every scenario passed and nothing tried to leave.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class HeadlessUI:
    """Nobody at the screen: questions get their first option, approvals are refused."""

    def __init__(self):
        self.asked, self.approvals = 0, 0

    async def queued(self, position):
        pass

    async def event(self, event):
        pass

    async def ask(self, question, options):
        self.asked += 1
        return options[0] if options else "(brak odpowiedzi — test bez użytkownika)"

    async def approve(self, tool, args):
        self.approvals += 1
        return False, "brak zgody (test bez użytkownika)"

    async def aborted(self, result):
        pass


def check(answer: str, checks: dict, files: list[dict]) -> list[str]:
    """v2's smoke checks on the answer; files are the ones the turn left in /praca/wyniki."""
    failed = []
    for needle in checks.get("contains", []):
        if needle not in answer:
            failed.append(f"missing '{needle}'")
    for needle in checks.get("not_contains", []):
        if needle in answer:
            failed.append(f"unexpected '{needle}'")
    for pattern in checks.get("regex", []):
        if not re.search(pattern, answer):
            failed.append(f"no match for /{pattern}/")
    if len(answer) < checks.get("min_chars", 0):
        failed.append(f"answer shorter than {checks['min_chars']} chars")
    if checks.get("pdf") and not any(f["name"].lower().endswith(".pdf") for f in files):
        failed.append("no PDF generated")
    if checks.get("file"):
        produced = [f for f in files if f["name"].lower().endswith(checks["file"])]
        if not produced:
            failed.append(f"no {checks['file']} file generated")
        else:
            from src.extractors import as_text, extract

            path = Path(produced[-1]["path"])
            text = as_text(extract(path))
            for needle in checks.get("file_contains", []):
                if needle not in text:
                    failed.append(f"{path.name} lacks '{needle}'")
            for needle in checks.get("file_not_contains", []):
                if needle in text:
                    failed.append(f"{path.name} still has '{needle}'")
    return failed


def refused(run_dir: Path) -> list[str]:
    """Connections the runner's egress guard refused in one turn (expected: none)."""
    path = run_dir / "egress.jsonl"
    out = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not entry.get("allowed", False):
                out.append(f"agent container: {entry.get('host')}:{entry.get('port')}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", help="agent profile (config/agent_profiles.yaml)")
    parser.add_argument("--scenarios", type=Path, default=ROOT / "tests" / "scenarios" / "smoke.yaml")
    parser.add_argument("--egress-guard", action="store_true")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--only", help="comma-separated scenario ids")
    parser.add_argument("--work", type=Path, help="keep the chats' workspaces (run folders, traces) here")
    args = parser.parse_args(argv)

    tmp = tempfile.TemporaryDirectory(prefix="hapilot-smoke-v3-")
    os.environ["HAPILOT_CHROMA_DIR"] = str(Path(tmp.name) / "chroma")
    os.environ["HAPILOT_ADDED_DOCS_DIR"] = str(Path(tmp.name) / "added_docs")
    os.environ["HAPILOT_AGENT_WORKSPACE_DIR"] = str((args.work or Path(tmp.name)) / "agent_workspace")
    from src.privacy import apply_offline_env, attempts, install_egress_guard

    apply_offline_env()
    if args.egress_guard:
        install_egress_guard()

    import yaml

    from src.agent_engine import AgentEngine, Turn, chat_prompt
    from src.agent_workspace import Workspace
    from src.config import get_settings, reset_settings
    from src.eval.agent import agent_model
    from src.ingest_data import ingest

    reset_settings()
    settings = get_settings()
    profile, model = agent_model(args.profile)
    settings = dataclasses.replace(settings, agent_model=model, agent_email=False)
    ingest(settings.docs_dir, settings.collection, added_dir=settings.added_docs_dir)
    engine = AgentEngine(settings)
    scenarios = yaml.safe_load(args.scenarios.read_text(encoding="utf-8"))["scenarios"]
    wanted = set(args.only.split(",")) if args.only else None
    run_id = uuid.uuid4().hex[:6]
    history: dict[str, list] = {}
    results, container_egress, skipped = [], [], []

    for sc in scenarios:
        if wanted and sc["id"] not in wanted:
            continue
        if "v3" not in sc.get("engines", ["v2", "v3"]):
            skipped.append(sc["id"])
            continue
        chat = f"smoke-v3-{run_id}-{sc.get('session', sc['id'])}"
        ws = Workspace(chat).prepare()
        uploads = [ws.add_upload(ROOT / a) for a in sc.get("attachments", [])]
        ui = HeadlessUI()
        t0 = time.monotonic()
        error, answer, files, tools, run_dir = None, "", [], {}, None
        try:
            result = asyncio.run(engine.run_turn(Turn(chat, chat_prompt(sc["prompt"], uploads),
                                                      history=history.get(chat, [])), ui))
            answer, files, tools, run_dir = result.text, result.files, result.tool_calls, result.run_dir
            if result.messages is not None:
                history[chat] = result.messages
            if result.status != "ok":
                error = f"status {result.status}: {result.error}"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        seconds = round(time.monotonic() - t0, 1)
        failed = [error] if error else check(answer, sc.get("checks", {}), files)
        leaks = refused(run_dir) if run_dir else []
        container_egress += leaks
        results.append({"id": sc["id"], "ok": not failed, "seconds": seconds, "failed": failed, "tools": tools,
                        "questions": ui.asked, "approvals_refused": ui.approvals,
                        "files": [f["name"] for f in files], "answer_excerpt": answer[:300],
                        "run": str(run_dir) if run_dir else None})
        print(f"{'PASS' if not failed else 'FAIL'}  {sc['id']:16s} {seconds:7.1f}s  {'; '.join(failed)}", flush=True)

    summary = {"passed": sum(r["ok"] for r in results), "total": len(results),
               "seconds_total": round(sum(r["seconds"] for r in results), 1), "skipped": skipped}
    describe = subprocess.run(["git", "describe", "--always", "--dirty", "--tags"], cwd=ROOT, capture_output=True,
                              text=True).stdout.strip()
    guard = [a["target"] for a in attempts()]
    report = {"engine": "v3", "profile": profile, "models": [model], "image": settings.agent_image, "commit": describe,
              "summary": summary, "results": results, "egress_guard_attempts": guard + container_egress,
              "container_egress": container_egress}
    print(json.dumps({"engine": "v3", "profile": profile, **summary, "egress_attempts": len(report["egress_guard_attempts"])}))
    out = args.report or settings.data_dir / "smoke" / f"smoke_v3_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.cleanup()
    return 0 if summary["passed"] == summary["total"] and not report["egress_guard_attempts"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
