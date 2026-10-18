#!/usr/bin/env python3
"""Check an evaluation run of Hapilot's own development tasks (tests/eval/dev_tasks.jsonl).

    .venv/bin/python scripts/dev_eval_check.py runs/<out_dir> [--expected tests/eval/dev_expected.json]

A development aid only: simple checks on Hapilot's own fictional fixtures. It is not the thesis
benchmark and is not used to score Hapilot there.
"""

import argparse
import json
from pathlib import Path


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace("zł", "").replace(" ", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def check(result: dict, exp: dict) -> list[str]:
    failed = []
    if result["status"] != "ok":
        return [f"status {result['status']}: {result['error']}"]
    answer = result.get("answer_json") or {}
    for field, rule in exp.get("json", {}).items():
        value = answer.get(field)
        if "number" in rule and (_number(value) is None or abs(_number(value) - rule["number"]) > 0.01):
            failed.append(f"{field}={value!r}, expected {rule['number']}")
        if "contains" in rule and rule["contains"].lower() not in str(value).lower():
            failed.append(f"{field}={value!r} lacks {rule['contains']!r}")
        if "bool" in rule and not (value is rule["bool"] or str(value).lower() == str(rule["bool"]).lower()):
            failed.append(f"{field}={value!r}, expected {rule['bool']}")
    for name in exp.get("sources_used", []):
        if not any(s.endswith(name) for s in result["sources_used"]):
            failed.append(f"source {name} not used")
    for tool in exp.get("plan_tools", []):
        if tool not in result["plan_tools"]:
            failed.append(f"tool {tool} not in plan {result['plan_tools']}")
    suffix = exp.get("files_created_suffix")
    if suffix and not any(f["path"].endswith(suffix) for f in result["files_created"]):
        failed.append(f"no {suffix} file created")
    return failed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--expected", type=Path, default=Path(__file__).resolve().parent.parent / "tests" / "eval" / "dev_expected.json")
    args = parser.parse_args()
    expected = json.loads(args.expected.read_text(encoding="utf-8"))
    results = {json.loads(l)["task_id"]: json.loads(l) for l in (args.out_dir / "results.jsonl").read_text().splitlines() if l.strip()}
    passed = 0
    for task_id, exp in expected.items():
        result = results.get(task_id)
        failed = ["not run"] if result is None else check(result, exp)
        passed += not failed
        extra = f"{result['duration_s']:6.1f}s llm={result['llm_calls']:2d} tools={result['plan_tools']}" if result else ""
        print(f"{'PASS' if not failed else 'FAIL'}  {task_id}  {extra}  {'; '.join(failed)}")
    print(f"{passed}/{len(expected)} passed")
    return 0 if passed == len(expected) else 1


if __name__ == "__main__":
    raise SystemExit(main())
