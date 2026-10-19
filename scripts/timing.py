#!/usr/bin/env python3
"""Time per path on the GPU host: the smoke scenarios repeated, the development tasks once, model loads.

    .venv/bin/python scripts/timing.py [--profile P ...] [--runs 3] [--out DIR] [--skip-dev]

For each profile (default: all) the model proxy is started (the first load is timed), the smoke scenarios
(tests/scenarios/smoke.yaml: one per path — chat, knowledge search, Python, documents, uploaded files) run
``--runs`` times, and the development tasks (tests/eval/dev_tasks.jsonl) run once through the evaluation
interface, which also records model calls and tokens. Model loads and swaps are read from the proxy's log.
Times are wall-clock seconds per request, planner included, measured by scripts/smoke.py and src/eval.
Writes timing_report.md and timing_report.json. Stops what it started.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import platform
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# What each smoke scenario exercises (the planner may still choose other tools; the report lists the plans seen).
PATHS = {
    "chat": "conversation, no tools",
    "rag_leave": "knowledge search, short answer",
    "rag_complaints": "knowledge search, two facts",
    "python_sum": "Python in the sandbox",
    "email_no_pdf": "writer: e-mail text",
    "pdf_letter": "document: letter as PDF",
    "pdf_modify": "edit the generated PDF",
    "file_summary": "summary of an uploaded 10-page PDF",
    "file_edit": "edit an uploaded Word file",
    "file_to_knowledge": "add an uploaded file to the knowledge base",
    "rag_added_document": "knowledge search incl. the added file",
}
_READY = re.compile(r"\[modelswap\] (\S+) ready after ([\d.]+)s")


def model_loads(log_text: str) -> list[tuple[str, float]]:
    """(model, seconds) for every model (re)load the proxy logged."""
    return [(m.group(1), float(m.group(2))) for m in _READY.finditer(log_text)]


def summarize_smoke(reports: list[dict]) -> list[dict]:
    """Per scenario: runs, passes, median/min/max seconds and the distinct plans, in scenario order."""
    order: list[str] = []
    by_id: dict[str, list[dict]] = {}
    for report in reports:
        for r in report["results"]:
            if r["id"] not in by_id:
                order.append(r["id"])
            by_id.setdefault(r["id"], []).append(r)
    rows = []
    for sid in order:
        runs = by_id[sid]
        secs = [r["seconds"] for r in runs]
        plans = []
        for r in runs:
            if r.get("plan_tools") not in plans:
                plans.append(r.get("plan_tools"))
        rows.append({"id": sid, "path": PATHS.get(sid, ""), "runs": len(runs), "passed": sum(r["ok"] for r in runs),
                     "median_s": round(statistics.median(secs), 1), "min_s": min(secs), "max_s": max(secs), "plans": plans})
    return rows


def summarize_dev(results: list[dict]) -> list[dict]:
    return [{"id": r["task_id"], "status": r["status"], "seconds": r["duration_s"], "llm_calls": r["llm_calls"],
             "prompt_tokens": r["prompt_tokens"], "completion_tokens": r["completion_tokens"], "plan": r["plan_tools"]}
            for r in sorted(results, key=lambda r: r["task_id"])]


def summarize_loads(loads: list[tuple[str, float]]) -> list[dict]:
    rows = {}
    for model, secs in loads:
        rows.setdefault(model, []).append(secs)
    return [{"model": m, "loads": len(v), "first_s": v[0], "median_s": round(statistics.median(v), 1), "max_s": max(v)}
            for m, v in rows.items()]


def render_markdown(report: dict) -> str:
    env = report["environment"]
    lines = [f"# Timing — {report['date']}", "",
             f"Commit `{report['git']['describe']}`; {env['gpu']}; {env['cpu']}; {env['ram_gb']} GB RAM; Python {env['python']}.",
             f"Smoke scenarios repeated {report['runs']}×; times are wall-clock seconds per request (planner included).", ""]
    for profile, data in report["profiles"].items():
        lines += [f"## {profile} ({', '.join(data['models'])})", "",
                  "| Path | Scenario | Passed | Median (s) | Min (s) | Max (s) | Plans seen |", "|---|---|---|---|---|---|---|"]
        for r in data["smoke"]:
            plans = "; ".join(" → ".join(p or ["—"]) for p in r["plans"])
            lines.append(f"| {r['path']} | `{r['id']}` | {r['passed']}/{r['runs']} | {r['median_s']} | {r['min_s']} | "
                         f"{r['max_s']} | {plans} |")
        if data.get("dev"):
            dev = data["dev"]
            lines += ["", "| Development task | Status | Time (s) | Model calls | Prompt tokens | Completion tokens | Plan |",
                      "|---|---|---|---|---|---|---|"]
            lines += [f"| {d['id']} | {d['status']} | {d['seconds']} | {d['llm_calls']} | {d['prompt_tokens']} | "
                      f"{d['completion_tokens']} | {' → '.join(d['plan'])} |" for d in dev]
            secs = [d["seconds"] for d in dev]
            lines.append(f"| **all {len(dev)}** | | **{round(sum(secs), 1)}** (median {round(statistics.median(secs), 1)}) | "
                         f"{sum(d['llm_calls'] for d in dev)} | {sum(d['prompt_tokens'] for d in dev)} | "
                         f"{sum(d['completion_tokens'] for d in dev)} | |")
        lines += ["", "| Model | Loads | First (s) | Median (s) | Max (s) |", "|---|---|---|---|---|"]
        lines += [f"| {m['model']} | {m['loads']} | {m['first_s']} | {m['median_s']} | {m['max_s']} |" for m in data["model_loads"]]
        lines.append("")
    return "\n".join(lines)


def _environment() -> dict:
    def run(cmd: list[str]) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            return ""

    gpu = run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"]) or "no GPU found"
    cpu = next((line.split(":", 1)[1].strip() for line in run(["lscpu"]).splitlines() if line.startswith("Model name")), "")
    mem = next((line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemTotal")), "0")
    return {"gpu": gpu, "cpu": cpu, "ram_gb": round(int(mem) / 1024 / 1024), "python": platform.python_version()}


def main(argv: list[str] | None = None) -> int:
    import hapilotctl
    from src.config import get_settings

    parser = argparse.ArgumentParser(description="Time per path on the GPU host")
    parser.add_argument("--profile", action="append")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--skip-dev", action="store_true")
    args = parser.parse_args(argv)

    settings = get_settings()
    out = args.out or settings.data_dir / "timing" / dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    py, proxy = hapilotctl.PY, f"http://127.0.0.1:{hapilotctl.PROXY_PORT}/v1"
    if hapilotctl.owned("modelswap") or hapilotctl.owned("ui"):
        raise SystemExit("[timing] Hapilot is already running here — stop it first (hapilotctl.py down)")
    proxy_log = hapilotctl.log_dir() / "modelswap.log"

    report = {"date": dt.datetime.now().isoformat(timespec="seconds"), "git": hapilotctl.git_state(), "runs": args.runs,
              "environment": _environment(), "profiles": {}}
    for profile in args.profile or list(settings.profiles):
        pout = out / profile
        pout.mkdir(parents=True, exist_ok=True)
        offset = proxy_log.stat().st_size if proxy_log.is_file() else 0
        data: dict = {"models": sorted({m.model for m in settings.profile(profile).models.values()})}
        t0 = time.monotonic()
        hapilotctl.models_up(profile)
        data["startup_s"] = round(time.monotonic() - t0, 1)
        try:
            reports = []
            for n in range(1, args.runs + 1):
                path = pout / f"smoke_{n}.json"
                with (pout / f"smoke_{n}.log").open("w", encoding="utf-8") as log:
                    subprocess.run([str(py), "scripts/smoke.py", "--model-url", proxy, "--profile", profile, "--egress-guard",
                                    "--report", str(path)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
                if path.is_file():
                    reports.append(json.loads(path.read_text(encoding="utf-8")))
                hapilotctl.say(f"{profile} smoke run {n}/{args.runs} done")
            data["smoke"] = summarize_smoke(reports)
            if not args.skip_dev:
                run_out = pout / "dev_eval"
                with (pout / "dev_eval.log").open("w", encoding="utf-8") as log:
                    subprocess.run([str(py), "-m", "src.eval", "run", "--tasks", "tests/eval/dev_tasks.jsonl", "--corpus",
                                    "examples/demo_company/docs", "--today", "2026-10-14", "--profile", profile, "--out",
                                    str(run_out)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                   env={**__import__("os").environ, "HAPILOT_MODEL_URL": proxy})
                results_file = run_out / "results.jsonl"
                if results_file.is_file():
                    data["dev"] = summarize_dev([json.loads(x) for x in results_file.read_text(encoding="utf-8").splitlines() if x])
        finally:
            hapilotctl.models_down()
        text = proxy_log.read_text(encoding="utf-8", errors="replace")[offset:] if proxy_log.is_file() else ""
        data["model_loads"] = summarize_loads(model_loads(text))
        report["profiles"][profile] = data

    (out / "timing_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "timing_report.md").write_text(render_markdown(report), encoding="utf-8")
    hapilotctl.say(f"timing report: {out / 'timing_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
