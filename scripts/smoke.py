#!/usr/bin/env python3
"""Run the smoke scenarios (tests/scenarios/smoke.yaml) headless against the real models.

    .venv/bin/python scripts/smoke.py --model-url http://127.0.0.1:8611/v1 [--profile P] [--egress-guard] [--report FILE]

Uses the ``auto`` approval policy (no forms, no plan approval) and one workspace per scenario
session. With --egress-guard every non-local connection attempt from this process is blocked and
listed in the report. Exit code 0 only if every scenario passed and no egress was attempted.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def check(answer: str, checks: dict, workspace: Path) -> list[str]:
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
    if checks.get("pdf"):
        match = re.search(r"Wygenerowano plik:\s*(\S+\.pdf)", answer)
        if not match or not (workspace / match.group(1)).is_file():
            failed.append("no PDF generated")
    return failed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-url", default=os.environ.get("HAPILOT_MODEL_URL", "http://127.0.0.1:8611/v1"))
    parser.add_argument("--profile")
    parser.add_argument("--scenarios", type=Path, default=ROOT / "tests" / "scenarios" / "smoke.yaml")
    parser.add_argument("--egress-guard", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    os.environ["HAPILOT_MODEL_URL"] = args.model_url
    from src.privacy import apply_offline_env, attempts, install_egress_guard

    apply_offline_env()
    if args.egress_guard:
        install_egress_guard()

    import yaml
    from langchain_core.messages import HumanMessage

    from src import run_context, sandbox
    from src.agent_graph import get_app
    from src.config import get_settings

    settings = get_settings()
    profile = settings.profile(args.profile)
    app = get_app()
    scenarios = yaml.safe_load(args.scenarios.read_text(encoding="utf-8"))["scenarios"]
    run_id = uuid.uuid4().hex[:6]
    sessions: dict[str, dict] = {}
    results = []

    for sc in scenarios:
        sid = f"smoke-{run_id}-{sc.get('session', sc['id'])}"
        sess = sessions.setdefault(sid, {"messages": [], "last_generated_file": ""})
        sess["messages"].append(HumanMessage(content=sc["prompt"]))
        inputs = {"messages": list(sess["messages"]), "next_step": "", "execution_status": "", "retry_count": 0,
                  "task_plan": {}, "task_id": "", "is_complex_task": False, "current_step": None, "generated_code": None,
                  "last_generated_file": sess["last_generated_file"], "hitl_request": None, "hitl_response": None,
                  "halt": False}
        config = run_context.make_config(sid, profile=profile.name, approval_policy="auto")
        t0 = time.monotonic()
        error = None
        try:
            state = app.invoke(inputs, config=config)
            answer = state["messages"][-1].content
            sess["messages"] = list(state["messages"])
            sess["last_generated_file"] = state.get("last_generated_file") or sess["last_generated_file"]
            tools = [s.get("tool") for s in (state.get("task_plan") or {}).get("steps", [])]
        except Exception as exc:
            answer, tools, error = "", [], f"{type(exc).__name__}: {exc}"
        seconds = round(time.monotonic() - t0, 1)
        with run_context.use_context(session_id=sid):
            workspace = run_context.workspace_dir()
        failed = [error] if error else check(answer, sc.get("checks", {}), workspace)
        results.append({"id": sc["id"], "ok": not failed, "seconds": seconds, "failed": failed,
                        "plan_tools": tools, "answer_excerpt": answer[:300]})
        print(f"{'PASS' if not failed else 'FAIL'}  {sc['id']:16s} {seconds:7.1f}s  {'; '.join(failed)}", flush=True)

    summary = {"passed": sum(r["ok"] for r in results), "total": len(results),
               "seconds_total": round(sum(r["seconds"] for r in results), 1)}
    describe = subprocess.run(["git", "describe", "--always", "--dirty", "--tags"], cwd=ROOT, capture_output=True,
                              text=True).stdout.strip()
    report = {"profile": profile.name, "models": sorted({m.model for m in profile.models.values()}), "commit": describe,
              "summary": summary, "results": results, "egress_guard_attempts": [a["target"] for a in attempts()],
              "sandbox_runs": sandbox.history}
    print(json.dumps({"profile": profile.name, **summary, "egress_attempts": len(report["egress_guard_attempts"])}))
    out = args.report or settings.data_dir / "smoke" / f"smoke_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if summary["passed"] == summary["total"] and not report["egress_guard_attempts"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
