"""Check an evaluation output directory against the JSON Schemas in docs/schemas/."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from src.config import PROJECT_ROOT

SCHEMAS = PROJECT_ROOT / "docs" / "schemas"


def _schema(name: str) -> dict:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


def validate_run(out_dir: Path) -> list[str]:
    """Problems found (empty list = valid)."""
    out_dir = Path(out_dir)
    problems: list[str] = []

    def check(obj, schema, where):
        for err in jsonschema.Draft202012Validator(schema).iter_errors(obj):
            problems.append(f"{where}: {'/'.join(map(str, err.absolute_path)) or '(root)'}: {err.message}")

    run_file = out_dir / "run.json"
    if not run_file.is_file():
        return [f"{run_file} missing"]
    run = json.loads(run_file.read_text(encoding="utf-8"))
    check(run, _schema("run"), "run.json")

    result_schema, trace_schema = _schema("result"), _schema("trace")
    results_file = out_dir / "results.jsonl"
    lines = results_file.read_text(encoding="utf-8").splitlines() if results_file.is_file() else []
    for n, line in enumerate(lines, 1):
        rec = json.loads(line)
        check(rec, result_schema, f"results.jsonl:{n}")
        if rec.get("run_id") != run.get("run_id"):
            problems.append(f"results.jsonl:{n}: run_id differs from run.json")
        trace = out_dir / rec.get("trace", "")
        if not trace.is_file():
            problems.append(f"results.jsonl:{n}: trace {rec.get('trace')} missing")
            continue
        last_seq = 0
        for m, tline in enumerate(trace.read_text(encoding="utf-8").splitlines(), 1):
            event = json.loads(tline)
            where = f"{trace.relative_to(out_dir)}:{m}"
            check(event, trace_schema, where)
            if event.get("seq", 0) <= last_seq:
                problems.append(f"{where}: seq not increasing")
            last_seq = event.get("seq", 0)
            for key in ("messages_ref", "response_ref"):
                ref = event.get(key)
                if ref and not (out_dir / "blobs" / f"{ref.split(':', 1)[1]}.json").is_file():
                    problems.append(f"{where}: {key} {ref} not in blobs/")
            if event.get("task_id") != rec["task_id"]:
                problems.append(f"{where}: task_id differs from the result line")
    return problems
