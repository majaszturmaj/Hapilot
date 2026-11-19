#!/usr/bin/env python3
"""Integration suite on the GPU host: unit tests, then per profile smoke, planner check and development tasks.

    .venv/bin/python scripts/hapilotctl.py integration [--profile P ...] [--out DIR] [--skip SUITE ...]
    .venv/bin/python scripts/hapilotctl.py integration --engine v3 [--profile P ...]

For each profile (default: every profile in config/profiles.yaml) the model proxy is started and three
suites run with the real models (each indexes what it needs into its own temporary or run-specific index):

- smoke    tests/scenarios/smoke.yaml through the whole graph (scripts/smoke.py, egress guard on)
- planner  tests/scenarios/planner.yaml, planner node only (scripts/planner_check.py)
- dev      tests/eval/dev_tasks.jsonl through the evaluation interface, schema validation and the checker
           (scripts/dev_eval_check.py), fixed date 2026-10-14

With ``--engine v3`` (the agent container, src/agent_engine.py) the profiles are those of config/agent_profiles.yaml
and the suites are:

- egress   the agent container cannot get out; the relay serves the profile's model (scripts/egress_v3.py). When the
           model is not served the profile's other suites are not run.
- smoke    the same scenarios through the chat engine (scripts/smoke_v3.py; `engines: [v2]` ones are skipped)
- dev      the development tasks through the evaluation interface with ``--engine v3``

v3 does not start the model: the operator serves it and the relay of the agent network (one model at a time; on the
GPU host of the pilot, window.sh). There is no planner in v3, so no planner suite.

Failures listed in tests/integration/baseline.yaml (known, recorded with the run that found them; v3 profiles under
``v3/<profile>``) are reported but do not fail the suite; any other failure, an invalid run, or an egress attempt
does. Writes integration_report.json and integration_report.md to the output directory. Stops what it started.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

BASELINE = ROOT / "tests" / "integration" / "baseline.yaml"
DEV_TASKS = ROOT / "tests" / "eval" / "dev_tasks.jsonl"
DEV_EXPECTED = ROOT / "tests" / "eval" / "dev_expected.json"
DEV_TODAY = "2026-10-14"
SUITES = ("smoke", "planner", "dev")
SUITES_V3 = ("egress", "smoke", "dev")


def baseline_key(engine: str, profile: str) -> str:
    """v3 profiles are named apart from v2's (both have a best-local)."""
    return f"v3/{profile}" if engine == "v3" else profile


# --------------------------------------------------------------------------- comparison with the baseline

def load_baseline(path: Path = BASELINE) -> dict:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {profile: {suite: set(ids or []) for suite, ids in (suites or {}).items()}
            for profile, suites in (data.get("known_failures") or {}).items()}


def compare(outcomes: dict[str, bool], known: set[str]) -> dict:
    """Classify item outcomes ({id: passed}) against known failures."""
    failed = {i for i, ok in outcomes.items() if not ok}
    return {"passed": sum(outcomes.values()), "total": len(outcomes),
            "regressions": sorted(failed - known),          # failed and not known: fails the suite
            "known_failures": sorted(failed & known),      # failed as recorded
            "now_passing": sorted(known & {i for i, ok in outcomes.items() if ok})}  # baseline can be tightened


def suite_ok(result: dict) -> bool:
    if result.get("skipped"):
        return False  # a suite that could not run (the model was not served) has not passed
    return (not result.get("error") and not result["comparison"]["regressions"] and not result.get("egress_attempts")
            and result.get("valid", True))


# --------------------------------------------------------------------------- suites

def _run(cmd: list[str], log: Path, env: dict | None = None) -> int:
    import os

    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as fh:
        return subprocess.run(cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT, env={**os.environ, **(env or {})}).returncode


def run_unit(out: Path, py: Path) -> dict:
    t0 = time.monotonic()
    log = out / "unit.log"
    code = _run([str(py), "-m", "pytest", "-q", "-p", "no:cacheprovider"], log)
    tail = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1:] or [""]
    return {"ok": code == 0, "summary": tail[0], "seconds": round(time.monotonic() - t0, 1)}


def run_smoke(profile: str, out: Path, py: Path, proxy: str | None, known: set[str], engine: str = "v2") -> dict:
    report = out / "smoke.json"
    if engine == "v3":
        cmd = [str(py), "scripts/smoke_v3.py", "--profile", profile, "--work", str(out / "smoke_work")]
    else:
        cmd = [str(py), "scripts/smoke.py", "--model-url", proxy, "--profile", profile]
    code = _run([*cmd, "--egress-guard", "--report", str(report)], out / "smoke.log")
    if not report.is_file():
        return {"error": f"no report (exit {code})", "comparison": compare({}, known)}
    data = json.loads(report.read_text(encoding="utf-8"))
    return {"comparison": compare({r["id"]: r["ok"] for r in data["results"]}, known),
            "seconds": data["summary"]["seconds_total"], "egress_attempts": data["egress_guard_attempts"],
            "failures": {r["id"]: r["failed"] for r in data["results"] if not r["ok"]}}


def run_planner(profile: str, out: Path, py: Path, proxy: str, known: set[str], repeat: int) -> dict:
    report = out / "planner.json"
    code = _run([str(py), "scripts/planner_check.py", "--model-url", proxy, "--profile", profile, "--repeat", str(repeat),
                 "--report", str(report)], out / "planner.log")
    if not report.is_file():
        return {"error": f"no report (exit {code})", "comparison": compare({}, known)}
    data = json.loads(report.read_text(encoding="utf-8"))
    return {"comparison": compare({r["id"]: r["ok"] for r in data["results"]}, known),
            "seconds": data["summary"]["seconds_total"], "egress_attempts": data["egress_guard_attempts"],
            "failures": {r["id"]: [f"{x['tools']}: {'; '.join(x['failed'])}" for x in r["runs"] if x["failed"]]
                         for r in data["results"] if not r["ok"]}}


def run_dev(profile: str, out: Path, py: Path, proxy: str | None, known: set[str], engine: str = "v2") -> dict:
    import dev_eval_check

    run_out = out / "dev_eval"
    code = _run([str(py), "-m", "src.eval", "run", "--tasks", str(DEV_TASKS), "--corpus", "examples/demo_company/docs",
                 "--today", DEV_TODAY, "--profile", profile, "--engine", engine, "--out", str(run_out)], out / "dev_eval.log",
                env={"HAPILOT_MODEL_URL": proxy} if proxy else None)
    results_file = run_out / "results.jsonl"
    if not results_file.is_file():
        return {"error": f"no results (exit {code})", "comparison": compare({}, known)}
    valid = _run([str(py), "-m", "src.eval", "validate", str(run_out)], out / "dev_validate.log") == 0
    results = {r["task_id"]: r for r in map(json.loads, results_file.read_text(encoding="utf-8").splitlines()) if r}
    expected = json.loads(DEV_EXPECTED.read_text(encoding="utf-8"))
    failures = {tid: (["not run"] if tid not in results else dev_eval_check.check(results[tid], exp, engine=engine))
                for tid, exp in expected.items()}
    egress = run_out / "egress.jsonl"
    return {"comparison": compare({tid: not f for tid, f in failures.items()}, known), "valid": valid,
            "seconds": round(sum(r.get("duration_s", 0) for r in results.values()), 1),
            "egress_attempts": [ln for ln in egress.read_text().splitlines() if ln.strip()] if egress.is_file() else [],
            "failures": {tid: f for tid, f in failures.items() if f}}


def run_egress_v3(profile: str, out: Path, py: Path, known: set[str]) -> dict:
    """The container's egress test; ``model_served`` says whether the profile's other suites can run."""
    report = out / "egress.json"
    code = _run([str(py), "scripts/egress_v3.py", "--profile", profile, "--report", str(report)], out / "egress.log")
    if not report.is_file():
        return {"error": f"no report (exit {code})", "comparison": compare({}, known), "model_served": False}
    data = json.loads(report.read_text(encoding="utf-8"))
    checks = {c["check"]: c["ok"] for c in data["checks"]}
    return {"comparison": compare(checks, known), "image_id": data.get("image_id"),
            "model_served": any(ok for name, ok in checks.items() if name.startswith("model relay")),
            "failures": {c["check"]: [str(c["detail"])] for c in data["checks"] if not c["ok"]}}


# --------------------------------------------------------------------------- report

def render_markdown(report: dict) -> str:
    engine = report.get("engine", "v2")
    lines = [f"# Integration report — {report['date']}{' (engine v3)' if engine == 'v3' else ''}", "",
             f"Commit `{report['git']['describe']}` ({report['git']['commit'][:12]}{', dirty' if report['git']['dirty'] else ''}). "
             f"Unit tests: {report['unit']['summary'] if report.get('unit') else 'skipped'}.", "",
             "| Profile | Suite | Passed | Known failures | Regressions | Valid | Egress | Time (s) |",
             "|---|---|---|---|---|---|---|---|"]
    for profile, suites in report["profiles"].items():
        for name, r in suites.items():
            if name == "models":
                continue
            if r.get("skipped"):
                lines.append(f"| {profile} | {name} | skipped: {r['skipped']} | — | — | — | — | — |")
                continue
            c = r["comparison"]
            lines.append(f"| {profile} | {name} | {c['passed']}/{c['total']}{' (' + r['error'] + ')' if r.get('error') else ''} | "
                         f"{', '.join(c['known_failures']) or '—'} | {', '.join(c['regressions']) or '—'} | "
                         f"{'yes' if r.get('valid', True) else 'NO'} | {len(r.get('egress_attempts', []))} | {r.get('seconds', '—')} |")
    lines += ["", f"**Result: {'PASSED' if report['passed'] else 'FAILED'}** (regressions, invalid runs, errors or egress "
              "attempts fail the suite; known failures are listed in tests/integration/baseline.yaml).", ""]
    details = [(p, n, r) for p, s in report["profiles"].items() for n, r in s.items() if n != "models" and r.get("failures")]
    if details:
        lines += ["## Failures", ""]
        for profile, name, r in details:
            for item, why in r["failures"].items():
                lines.append(f"- {profile} / {name} / {item}: {'; '.join(map(str, why))[:300]}")
        lines.append("")
    tightened = [(p, n, r["comparison"]["now_passing"]) for p, s in report["profiles"].items() for n, r in s.items()
                 if n != "models" and not r.get("skipped") and r["comparison"]["now_passing"]]
    if tightened:
        lines += ["## Known failures that passed this time", ""]
        lines += [f"- {p} / {n}: {', '.join(ids)}" for p, n, ids in tightened] + [""]
    return "\n".join(lines)


def run_profile_v3(profile: str, model: str, out: Path, py: Path, known: dict, skip: list[str]) -> dict:
    results: dict = {"models": [model]}
    if "egress" not in skip:
        results["egress"] = run_egress_v3(profile, out, py, known.get("egress", set()))
        if not results["egress"]["model_served"]:
            why = f"{model} not served at the agent relay"
            for name in ("smoke", "dev"):
                if name not in skip:
                    results[name] = {"skipped": why, "comparison": compare({}, set())}
            return results
    if "smoke" not in skip:
        results["smoke"] = run_smoke(profile, out, py, None, known.get("smoke", set()), engine="v3")
    if "dev" not in skip:
        results["dev"] = run_dev(profile, out, py, None, known.get("dev", set()), engine="v3")
    return results


def main(argv: list[str] | None = None) -> int:
    import hapilotctl
    from src.config import get_settings, load_agent_profiles

    parser = argparse.ArgumentParser(description="Hapilot integration suite (GPU host)")
    parser.add_argument("--engine", choices=["v2", "v3"], default="v2")
    parser.add_argument("--profile", action="append", help="profile to test (repeatable; default: all)")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--skip", action="append", default=[], choices=["unit", *SUITES, "egress"])
    parser.add_argument("--planner-repeat", type=int, default=1)
    args = parser.parse_args(argv)

    settings = get_settings()
    agent_profiles = load_agent_profiles()[0] if args.engine == "v3" else {}
    profiles = args.profile or list(agent_profiles if args.engine == "v3" else settings.profiles)
    unknown = [p for p in profiles if args.engine == "v3" and p not in agent_profiles]
    if unknown:
        raise SystemExit(f"[integration] unknown agent profile(s): {', '.join(unknown)}")
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = args.out or settings.data_dir / "integration" / (f"v3_{stamp}" if args.engine == "v3" else stamp)
    out.mkdir(parents=True, exist_ok=True)
    baseline = load_baseline()
    py, proxy = hapilotctl.PY, f"http://127.0.0.1:{hapilotctl.PROXY_PORT}/v1"
    if args.engine == "v2" and (hapilotctl.owned("modelswap") or hapilotctl.owned("ui")):
        raise SystemExit("[integration] Hapilot is already running here — stop it first (hapilotctl.py down)")

    report = {"date": dt.datetime.now().isoformat(timespec="seconds"), "engine": args.engine, "git": hapilotctl.git_state(),
              "profiles": {}}
    if "unit" not in args.skip:
        report["unit"] = run_unit(out, py)
        hapilotctl.say(f"unit tests: {report['unit']['summary']}")
    for profile in profiles:
        known = baseline.get(baseline_key(args.engine, profile), {})
        pout = out / profile
        if args.engine == "v3":
            results = run_profile_v3(profile, agent_profiles[profile]["model"], pout, py, known, args.skip)
        else:
            results = {"models": sorted({m.model for m in settings.profile(profile).models.values()})}
            hapilotctl.models_up(profile)
            try:
                if "smoke" not in args.skip:
                    results["smoke"] = run_smoke(profile, pout, py, proxy, known.get("smoke", set()))
                if "planner" not in args.skip:
                    results["planner"] = run_planner(profile, pout, py, proxy, known.get("planner", set()),
                                                     args.planner_repeat)
                if "dev" not in args.skip:
                    results["dev"] = run_dev(profile, pout, py, proxy, known.get("dev", set()))
            finally:
                hapilotctl.models_down()
        report["profiles"][profile] = results
        for name in SUITES_V3 if args.engine == "v3" else SUITES:
            if name in results:
                r = results[name]
                c = r["comparison"]
                hapilotctl.say(f"{profile} {name}: " + (f"skipped ({r['skipped']})" if r.get("skipped") else
                                                        f"{c['passed']}/{c['total']}, regressions {c['regressions'] or 'none'}"))

    report["passed"] = (report.get("unit", {"ok": True})["ok"]
                        and all(suite_ok(r) for s in report["profiles"].values() for n, r in s.items() if n != "models"))
    (out / "integration_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "integration_report.md").write_text(render_markdown(report), encoding="utf-8")
    hapilotctl.say(f"integration {'PASSED' if report['passed'] else 'FAILED'} — report: {out / 'integration_report.md'}")
    return 0 if report["passed"] else 1

if __name__ == "__main__":
    raise SystemExit(main())
