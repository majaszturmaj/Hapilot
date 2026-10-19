"""The timing script's parsing and summaries (the measurements themselves run on the GPU host)."""

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import timing  # noqa: E402


def test_every_smoke_scenario_has_a_path_description():
    scenarios = yaml.safe_load((ROOT / "tests" / "scenarios" / "smoke.yaml").read_text(encoding="utf-8"))["scenarios"]
    assert [sc["id"] for sc in scenarios] == list(timing.PATHS)


def test_model_loads_are_read_from_the_proxy_log():
    log = ("[modelswap] starting bielik-11b-v3-q8\n[modelswap] bielik-11b-v3-q8 ready after 2.0s\n"
           '[modelswap] "POST /v1/chat/completions HTTP/1.1" 200 -\n'
           "[modelswap] qwen3-coder-30b-a3b-q4km ready after 3.0s\n[modelswap] bielik-11b-v3-q8 ready after 2.4s\n")
    loads = timing.model_loads(log)
    assert loads == [("bielik-11b-v3-q8", 2.0), ("qwen3-coder-30b-a3b-q4km", 3.0), ("bielik-11b-v3-q8", 2.4)]
    assert timing.summarize_loads(loads) == [
        {"model": "bielik-11b-v3-q8", "loads": 2, "first_s": 2.0, "median_s": 2.2, "max_s": 2.4},
        {"model": "qwen3-coder-30b-a3b-q4km", "loads": 1, "first_s": 3.0, "median_s": 3.0, "max_s": 3.0}]


def _smoke(*rows):
    return {"results": [{"id": i, "ok": ok, "seconds": s, "plan_tools": p} for i, ok, s, p in rows]}


def test_smoke_runs_are_summarised_per_scenario():
    reports = [_smoke(("chat", True, 4.0, ["chat"]), ("rag_leave", True, 9.0, ["rag_search"])),
               _smoke(("chat", True, 6.0, ["chat"]), ("rag_leave", False, 7.0, ["rag_search", "llm_synthesis"])),
               _smoke(("chat", True, 5.0, ["chat"]), ("rag_leave", True, 8.0, ["rag_search"]))]
    rows = timing.summarize_smoke(reports)
    assert [r["id"] for r in rows] == ["chat", "rag_leave"]
    assert rows[1] == {"id": "rag_leave", "path": timing.PATHS["rag_leave"], "runs": 3, "passed": 2, "median_s": 8.0,
                       "min_s": 7.0, "max_s": 9.0, "plans": [["rag_search"], ["rag_search", "llm_synthesis"]]}


def test_report_lists_paths_tasks_and_loads():
    report = {"date": "2026-10-19T17:00:00", "git": {"describe": "v0.3.0"}, "runs": 3,
              "environment": {"gpu": "RTX 4090", "cpu": "Ryzen", "ram_gb": 124, "python": "3.12.3"},
              "profiles": {"best-local": {
                  "models": ["qwen3.6-27b-q4km"],
                  "smoke": timing.summarize_smoke([_smoke(("chat", True, 4.0, ["chat"]))]),
                  "dev": timing.summarize_dev([{"task_id": "D01", "status": "ok", "duration_s": 12.5, "llm_calls": 4,
                                                "prompt_tokens": 3000, "completion_tokens": 200, "plan_tools": ["rag_search"]}]),
                  "model_loads": timing.summarize_loads([("qwen3.6-27b-q4km", 3.2)])}}}
    text = timing.render_markdown(report)
    assert "| conversation, no tools | `chat` | 1/1 | 4.0 | 4.0 | 4.0 | chat |" in text
    assert "| D01 | ok | 12.5 | 4 | 3000 | 200 | rag_search |" in text and "| **all 1** | | **12.5** (median 12.5) |" in text
    assert "| qwen3.6-27b-q4km | 1 | 3.2 | 3.2 | 3.2 |" in text
