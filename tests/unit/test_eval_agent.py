"""Evaluation runs on the v3 engine (src/eval/agent.py): the same interface, a task per agent container. The container
is replaced by a small Python process that speaks the runner's side: trace and result files, the bridge socket for
search and the answer check (an invalid answer is refused once, then corrected), files left in /praca."""

import json
import sys
import textwrap

import pytest

from src import agent_engine, knowledge_base
from src.config import ConfigError, load_agent_profiles
from src.eval import EvalSession, RunConfig, Task
from src.eval import agent as eval_agent
from src.eval import runner
from src.eval.validate import validate_run

DOC = "Wniosek urlopowy składa się co najmniej 14 dni kalendarzowych przed urlopem."

FAKE_RUNNER = textwrap.dedent('''
    import json, socket, sys, time
    from pathlib import Path
    run, sock, praca, scenario = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), sys.argv[4]
    spec = json.loads((run / "spec.json").read_text())
    t0 = time.monotonic()

    def trace(**entry):
        with open(run / "trace.jsonl", "a") as fh:
            fh.write(json.dumps({"t": int((time.monotonic() - t0) * 1000), **entry}) + "\\n")

    def bridge(method, params):
        s = socket.socket(socket.AF_UNIX)
        s.connect(sock)
        s.sendall((json.dumps({"method": method, "params": params}) + "\\n").encode())
        data = b""
        while chunk := s.recv(65536):
            data += chunk
        return json.loads(data)

    (run / "egress.jsonl").write_text(json.dumps({"host": "model", "port": 8000, "allowed": True}) + "\\n"
                                      + (json.dumps({"host": "example.com", "port": 443, "allowed": False}) + "\\n"
                                         if scenario == "egress" else ""))
    if scenario == "sleep":
        time.sleep(30)
    if scenario == "crash":
        sys.exit(3)
    trace(type="system_prompt", chars=10, text="system")
    trace(type="usage", **{"in": 100, "out": 20})
    trace(type="tool_start", tool="read_files", id="c1", input={"files": [{"path": "/praca/tekst/kadry/regulamin.txt.txt"}]})
    trace(type="tool_end", tool="read_files", id="c1", output="treść " * 200, error=None, ms=4)
    trace(type="usage", **{"in": 150, "out": 30})
    trace(type="tool_start", tool="semantic_search", id="c2", input={"query": "wniosek urlopowy", "k": 3})
    found = bridge("semantic_search", {"query": "wniosek urlopowy", "k": 3})
    trace(type="tool_end", tool="semantic_search", id="c2", output=found["result"], error=None, ms=7)
    (praca / "wyniki").mkdir(exist_ok=True)
    (praca / "wyniki" / "notatka.md").write_text("notatka")
    if "answer" in spec:
        answer = Path(str(praca) + spec["answer"]["file"][len("/praca"):])
        for attempt, text in enumerate(('{"odpowiedz": 14}', '{"odpowiedz": 14, "zrodla": ["regulamin.txt"]}'), 1):
            answer.write_text(text)
            verdict = bridge("answer", {"text": text})["result"]
            accepted = verdict["valid"] or attempt > spec["answer"]["corrections"]
            trace(type="answer_check", attempt=attempt, file=True, valid=verdict["valid"], error=verdict["error"],
                  accepted=accepted)
            trace(type="tool_end", tool="submit_and_exit", id=f"s{attempt}", output=None,
                  error=None if accepted else "Odpowiedź końcowa nie została przyjęta", ms=1)
            if accepted:
                break
    result = {"status": "ok", "finish_reason": "completed", "text": "", "iterations": 3,
              "submitted": {"summary": "Wniosek 14 dni wcześniej.", "verified": True},
              "usage": {"inputTokens": 250, "outputTokens": 50}}
    (run / "result.json").write_text(json.dumps(result))
''')


class CorpusBackend:
    def dense(self, path, collection, query, k):
        return [("kadry/regulamin.txt#0:abc", DOC, {"source": "kadry/regulamin.txt", "chunk_id": "kadry/regulamin.txt#0:abc"}, 0.8)]

    def all_chunks(self, path, collection):
        return [("kadry/regulamin.txt#0:abc", DOC, {"source": "kadry/regulamin.txt", "chunk_id": "kadry/regulamin.txt#0:abc"})]


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    docs = tmp_path / "docs" / "kadry"
    docs.mkdir(parents=True)
    (docs / "regulamin.txt").write_text(DOC, encoding="utf-8")
    (tmp_path / "docs" / "inne.txt").write_text("inne", encoding="utf-8")
    monkeypatch.setattr(runner, "index_corpus", lambda d: {"fingerprint": "sha256:" + "0" * 64, "files": 2, "chunks": 2,
                                                          "collection": "eval_000000000000", "path": str(tmp_path / "idx"),
                                                          "skipped": []})
    knowledge_base.set_backend(CorpusBackend())
    fake = tmp_path / "fake_runner.py"
    fake.write_text(FAKE_RUNNER, encoding="utf-8")
    scenario = {"name": "ok"}

    def command(self, ws, run, dom, sock_dir, name):
        return [sys.executable, str(fake), str(run), str(sock_dir / agent_engine.SOCKET), str(ws.praca), scenario["name"]]

    monkeypatch.setattr(agent_engine.AgentEngine, "container_command", command)
    monkeypatch.setattr(eval_agent, "image_id", lambda image: "sha256:test")
    monkeypatch.setattr(eval_agent, "GRACE_S", 0)
    yield tmp_path / "docs", scenario
    knowledge_base.set_backend(None)


def _cfg(docs, tmp_path, **kw):
    return RunConfig(corpus_dir=docs, out_dir=tmp_path / "out", today="2026-10-14", engine="v3", **kw)


def test_task_with_answer_format_runs_in_the_container_and_keeps_the_interface(corpus, tmp_path):
    docs, _ = corpus
    task = Task(id="T1", instruction="Ile dni wcześniej składa się wniosek urlopowy?",
                answer_format={"fields": {"odpowiedz": "liczba dni", "zrodla": "lista plików"}})
    with EvalSession(_cfg(docs, tmp_path)) as run:
        result = run.run_task(task)
    out = tmp_path / "out"

    assert result["status"] == "ok" and result["error"] is None
    assert result["answer_json"] == {"odpowiedz": 14, "zrodla": ["regulamin.txt"]} and result["answer_json_valid"] is True
    assert result["answer_text"] == "Wniosek 14 dni wcześniej."
    assert result["sources_used"] == ["kadry/regulamin.txt"] and result["sources_cited"] == ["kadry/regulamin.txt"]
    assert result["plan_tools"] == ["read_files", "semantic_search", "submit_and_exit"]
    assert result["llm_calls"] == 2 and (result["prompt_tokens"], result["completion_tokens"]) == (250, 50)
    assert {f["path"] for f in result["files_created"]} == {"odpowiedz.json", "wyniki/notatka.md"}
    assert result["agent"]["answer_checks"] == 2 and result["agent"]["tool_calls"]["submit_and_exit"] == 2
    assert validate_run(out) == []

    meta = json.loads((out / "run.json").read_text())
    assert meta["config"]["engine"] == "v3" and meta["config"]["profile"] == "best-local"
    assert meta["agent"]["model"] == "qwen3.6-27b-q4km" and meta["agent"]["mode"] == "yolo"
    assert meta["agent"]["image_id"] == "sha256:test" and meta["agent"]["rules_sha256"]
    assert meta["models"] == [{"name": "qwen3.6-27b-q4km", "file": None, "bytes": None, "server": None}]

    events = [json.loads(line) for line in (out / "traces" / "T1.jsonl").read_text().splitlines()]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "task_start" and kinds[-1] == "task_end" and kinds.count("llm_call") == 2
    calls = [e for e in events if e["type"] == "tool_call"]
    assert [c["name"] for c in calls] == ["read_files", "semantic_search", "submit_and_exit", "submit_and_exit"]
    assert calls[0]["result_summary"].startswith("treść") and len(calls[0]["result_summary"]) == 500
    assert calls[2]["exception"] and calls[3]["exception"] is None
    retrieval = next(e for e in events if e["type"] == "retrieval")
    assert retrieval["query"] == "wniosek urlopowy" and retrieval["chunks"][0]["file"] == "kadry/regulamin.txt"
    checks = [e for e in events if e["type"] == "answer_format"]
    assert [(c["attempts"], c["valid"], c["accepted"]) for c in checks] == [(1, False, False), (2, True, True)]
    assert "missing fields: zrodla" in checks[0]["error"]

    spec = json.loads((out / "workspaces" / "T1" / "run" / "spec.json").read_text())
    assert spec["mode"] == "yolo" and spec["answer"]["corrections"] == 1 and spec["approval"]["ask"] == []
    assert "events" not in spec and "ask" not in spec and spec["modelId"] == "qwen3.6-27b-q4km"
    assert spec["prompt"].startswith("Dziś jest środa, 14 października 2026 r.")
    assert "- odpowiedz: liczba dni" in spec["prompt"] and "/praca/odpowiedz.json" in spec["prompt"]
    assert (out / "egress.jsonl").read_text() == ""


def test_attachments_and_a_task_without_format(corpus, tmp_path):
    docs, _ = corpus
    note = tmp_path / "zalacznik.txt"
    note.write_text("Delegacja: 120 zł", encoding="utf-8")
    task = Task(id="T2", instruction="Podsumuj załącznik.", attachments=[note])
    with EvalSession(_cfg(docs, tmp_path)) as run:
        result = run.run_task(task)
    assert result["status"] == "ok" and result["answer_json"] is None and result["answer_json_valid"] is None
    assert result["answer_text"] == "Wniosek 14 dni wcześniej." and "zalacznik.txt" in result["sources_used"]
    spec = json.loads((tmp_path / "out" / "workspaces" / "T2" / "run" / "spec.json").read_text())
    assert "answer" not in spec and "/praca/zalaczniki/zalacznik.txt (tekst:" in spec["prompt"]
    assert "submit_and_exit (pole summary)" in spec["prompt"]
    assert validate_run(tmp_path / "out") == []


def test_timeout_crash_and_egress_are_reported(corpus, tmp_path):
    docs, scenario = corpus
    task = Task(id="T3", instruction="x", answer_format={"fields": {"a": "b"}})
    scenario["name"] = "sleep"
    with EvalSession(_cfg(docs, tmp_path, task_timeout_s=1)) as run:
        slow = run.run_task(task)
        assert slow["status"] == "timeout" and "time budget" in slow["error"]
        assert slow["answer_json_valid"] is False and slow["answer_json_error"] == "no JSON object found"
        scenario["name"] = "crash"
        crashed = run.run_task(Task(id="T4", instruction="x"))
        assert crashed["status"] == "error" and crashed["error"] == "runner wrote no result"
        scenario["name"] = "egress"
        leaky = run.run_task(Task(id="T5", instruction="x"))
        assert leaky["agent"]["egress_blocked"] == 1
    lines = [json.loads(x) for x in (tmp_path / "out" / "egress.jsonl").read_text().splitlines()]
    assert lines == [{"task_id": "T5", "source": "agent container", "host": "example.com", "port": 443, "allowed": False}]
    assert validate_run(tmp_path / "out") == []
    meta = json.loads((tmp_path / "out" / "run.json").read_text())
    assert meta["tasks"] == {"total": 3, "ok": 1, "error": 1, "timeout": 1}


def test_agent_profiles_and_config(tmp_path, monkeypatch):
    profiles, default = load_agent_profiles()
    assert default == "best-local" and profiles["gemma"]["model"] == "gemma-4-31b-qat"
    assert eval_agent.agent_model(None) == ("best-local", "qwen3.6-27b-q4km")
    with pytest.raises(ConfigError):
        eval_agent.agent_model("nie-ma")
    with pytest.raises(ConfigError):
        RunConfig(corpus_dir=tmp_path, out_dir=tmp_path / "o", today="2026-10-14", engine="v4")
    v2 = RunConfig(corpus_dir=tmp_path, out_dir=tmp_path / "o", today="2026-10-14")
    assert "engine" not in v2.public()  # v2 runs keep their configuration hash
    bad = tmp_path / "p.yaml"
    bad.write_text("default: x\nprofiles:\n  x: {description: brak}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_agent_profiles(bad)
    bad.write_text("default: y\nprofiles:\n  x: {model: m}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_agent_profiles(bad)
    monkeypatch.setenv("HAPILOT_AGENT_PROFILES_FILE", str(tmp_path / "brak.yaml"))
    with pytest.raises(ConfigError):
        load_agent_profiles()


def test_cli_runs_engine_v3(corpus, tmp_path, capsys):
    from src.eval.__main__ import main

    docs, _ = corpus
    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text(json.dumps({"id": "C1", "instruction": "Ile dni?",
                                 "answer_format": {"fields": {"odpowiedz": "x", "zrodla": "y"}}}) + "\n", encoding="utf-8")
    code = main(["run", "--tasks", str(tasks), "--corpus", str(docs), "--out", str(tmp_path / "cli"), "--today",
                 "2026-10-14", "--engine", "v3", "--profile", "gemma"])
    assert code == 0
    meta = json.loads((tmp_path / "cli" / "run.json").read_text())
    assert meta["agent"]["model"] == "gemma-4-31b-qat" and meta["config"]["profile"] == "gemma"
    assert main(["validate", str(tmp_path / "cli")]) == 0
