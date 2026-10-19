#!/usr/bin/env python3
"""Check which tools the task planner chooses (tests/scenarios/planner.yaml) against the real models.

    .venv/bin/python scripts/planner_check.py --model-url http://127.0.0.1:8611/v1 [--profile P] [--repeat N] [--report FILE]

Runs only the planner node (with its guards), not the tools. Scenarios with ``attachments`` get the
attachment listing the UI adds to a message (the files themselves are not needed). Approval policy ``auto``. With --repeat N
each scenario is planned N times; it passes only if every run passes. Exit code 0 only if all passed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCENARIOS = ROOT / "tests" / "scenarios" / "planner.yaml"
CHECK_KEYS = {"require", "require_any", "forbid", "order"}
PRIOR_DOCUMENT = ("Przygotuj podanie o urlop wypoczynkowy od 3 do 7 listopada 2026.",
                  "SUKCES: Wygenerowano plik: podanie_urlop_20261019_120000.pdf")


def load_scenarios(path: Path = SCENARIOS) -> list[dict]:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))["scenarios"]


def validate_scenarios(scenarios: list[dict], known_tools: set[str]) -> list[str]:
    """Problems in the scenario file itself: missing fields, duplicate ids, unknown tools, no checks."""
    problems, seen = [], set()
    for i, sc in enumerate(scenarios):
        sid = sc.get("id") or f"#{i + 1}"
        if not sc.get("id") or not sc.get("prompt"):
            problems.append(f"{sid}: needs id and prompt")
        if sid in seen:
            problems.append(f"{sid}: duplicate id")
        seen.add(sid)
        if not CHECK_KEYS & set(sc):
            problems.append(f"{sid}: no checks")
        if len(sc.get("order", [])) not in (0, 2):
            problems.append(f"{sid}: order needs exactly two tools")
        named = list(sc.get("require", [])) + list(sc.get("forbid", [])) + list(sc.get("order", []))
        named += [t for group in sc.get("require_any", []) for t in group]
        for tool in named:
            if tool not in known_tools:
                problems.append(f"{sid}: unknown tool {tool!r}")
    return problems


def check_plan(tools: list[str], sc: dict) -> list[str]:
    """Failed checks for one planned tool sequence."""
    failed = []
    for tool in sc.get("require", []):
        if tool not in tools:
            failed.append(f"missing {tool}")
    for group in sc.get("require_any", []):
        if not any(t in tools for t in group):
            failed.append(f"none of {group}")
    for tool in sc.get("forbid", []):
        if tool in tools:
            failed.append(f"unexpected {tool}")
    order = sc.get("order") or []
    if len(order) == 2:
        a, b = order
        if a in tools and b in tools and tools.index(a) > tools.index(b):
            failed.append(f"{b} before {a}")
        elif a in tools and b not in tools or b in tools and a not in tools:
            failed.append(f"order {a} → {b} needs both")
    return failed


def plan_once(sc: dict, profile: str, session: str, trace_dir: Optional[Path] = None) -> tuple[list[str], list[str]]:
    """Run the planner node for one scenario; returns (tools in plan order, guards applied).

    With ``trace_dir`` the planner's model calls are traced there (``hapilot.trace/1``, one file per run).
    """
    import tempfile

    from langchain_core.messages import AIMessage, HumanMessage

    from src import run_context, tracing
    from src.agent_graph import task_planner_node
    from src.attachments import LISTING_PREFIX

    messages = []
    if sc.get("has_document"):
        messages += [HumanMessage(content=PRIOR_DOCUMENT[0]), AIMessage(content=PRIOR_DOCUMENT[1])]
    block = f"\n{LISTING_PREFIX}{', '.join(sc['attachments'])}" if sc.get("attachments") else ""
    messages.append(HumanMessage(content=sc["prompt"] + block))
    state = {"messages": messages, "last_generated_file": "podanie_urlop_20261019_120000.tex" if sc.get("has_document") else "",
             "hitl_response": None}
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(trace_dir) if trace_dir else Path(tmp)
        trace_file = base / f"{session}.jsonl"
        recorder = tracing.TraceRecorder(trace_file, base / "blobs", run_id=session, task_id=sc["id"])
        try:
            with run_context.use_context(session_id=session, profile=profile, approval_policy="auto",
                                         node="task_planner", trace=recorder):
                out = task_planner_node(state)
        finally:
            recorder.close()
        events = [json.loads(line) for line in trace_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    steps = (out.get("task_plan") or {}).get("steps", [])
    guards = [g for e in events if e["type"] == "plan" for g in e.get("guards_applied", [])]
    return [s.get("tool", "?") for s in steps], guards


def run(scenarios: list[dict], profile: str, repeat: int = 1, plan=plan_once, trace_dir: Optional[Path] = None) -> list[dict]:
    run_id = uuid.uuid4().hex[:6]
    results = []
    for sc in scenarios:
        runs = []
        for n in range(repeat):
            t0 = time.monotonic()
            try:
                tools, guards = plan(sc, profile, f"planner-{run_id}-{sc['id']}-{n}", trace_dir)
                failed = check_plan(tools, sc)
            except Exception as exc:  # a crash is a failure of this scenario, not of the whole check
                tools, guards, failed = [], [], [f"{type(exc).__name__}: {exc}"]
            runs.append({"tools": tools, "guards": guards, "failed": failed, "seconds": round(time.monotonic() - t0, 1)})
        ok = all(not r["failed"] for r in runs)
        results.append({"id": sc["id"], "ok": ok, "runs": runs})
        last = runs[-1]
        print(f"{'PASS' if ok else 'FAIL'}  {sc['id']:24s} {last['seconds']:6.1f}s  {last['tools']}  "
              f"{'; '.join(f for r in runs for f in r['failed'])}", flush=True)
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-url", default=os.environ.get("HAPILOT_MODEL_URL", "http://127.0.0.1:8611/v1"))
    parser.add_argument("--profile")
    parser.add_argument("--scenarios", type=Path, default=SCENARIOS)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--only", help="comma-separated scenario ids")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    os.environ["HAPILOT_MODEL_URL"] = args.model_url
    from src.privacy import apply_offline_env, attempts, install_egress_guard

    apply_offline_env()
    install_egress_guard()

    from src.agent_graph import _build_dynamic_tool_map
    from src.config import get_settings

    profile = get_settings().profile(args.profile).name
    scenarios = load_scenarios(args.scenarios)
    problems = validate_scenarios(scenarios, set(_build_dynamic_tool_map()))
    if problems:
        print("\n".join(problems))
        return 2
    if args.only:
        wanted = set(args.only.split(","))
        scenarios = [sc for sc in scenarios if sc["id"] in wanted]
    trace_dir = args.report.parent / "planner_traces" if args.report else None
    results = run(scenarios, profile, args.repeat, trace_dir=trace_dir)
    summary = {"passed": sum(r["ok"] for r in results), "total": len(results), "repeat": args.repeat,
               "seconds_total": round(sum(x["seconds"] for r in results for x in r["runs"]), 1)}
    report = {"profile": profile, "summary": summary, "results": results,
              "egress_guard_attempts": [a["target"] for a in attempts()]}
    print(json.dumps({"profile": profile, **summary, "egress_attempts": len(report["egress_guard_attempts"])}))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if summary["passed"] == summary["total"] and not report["egress_guard_attempts"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
