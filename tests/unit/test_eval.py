"""Evaluation interface v1 end to end with a scripted model, fake Docker and a fake knowledge base."""

import io
import json

import pytest

from src import knowledge_base
from src.config import ConfigError
from src.eval import EvalSession, RunConfig, Task, load_tasks
from src.eval import formatter, runner
from src.eval.validate import validate_run

DOC = "Wniosek urlopowy składa się co najmniej 14 dni kalendarzowych przed urlopem."


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
    monkeypatch.setattr(runner, "index_corpus", lambda d: {"fingerprint": "sha256:" + "0" * 64, "files": 1, "chunks": 1,
                                                          "collection": "eval_000000000000", "path": str(tmp_path / "idx"),
                                                          "skipped": []})
    knowledge_base.set_backend(CorpusBackend())
    yield tmp_path / "docs"
    knowledge_base.set_backend(None)


def plan(*steps):
    return json.dumps({"task_name": "t", "complexity": "low",
                       "steps": [{"id": i + 1, "action": t, "tool": t, "input": inp} for i, (t, inp) in enumerate(steps)]})


def _cfg(corpus, tmp_path, **kw):
    return RunConfig(corpus_dir=corpus, out_dir=tmp_path / "out", today="2026-10-14", **kw)


def _xlsx(path):
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.append(["Nr", "Kwota"])
    wb.active.append(["R-1", 1500])
    wb.active.append(["R-2", 320])
    buf = io.BytesIO()
    wb.save(buf)
    path.write_bytes(buf.getvalue())


def test_rag_task_with_answer_format(corpus, tmp_path, llm, docker):
    llm.on("task_planner", plan(("rag_search", "termin wniosku urlopowego")))
    llm.on("knowledge", "wniosek urlopowy termin", "Wniosek składa się 14 dni wcześniej [regulamin.txt].")
    llm.on("answer_formatter", '{"odpowiedz": 14, "zrodla": ["regulamin.txt"]}')
    task = Task(id="T1", instruction="Ile dni wcześniej składa się wniosek urlopowy?",
                answer_format={"fields": {"odpowiedz": "liczba dni", "zrodla": "lista plików"}})
    with EvalSession(_cfg(corpus, tmp_path)) as run:
        result = run.run_task(task)
    out = tmp_path / "out"

    assert result["status"] == "ok" and result["answer_json"] == {"odpowiedz": 14, "zrodla": ["regulamin.txt"]}
    assert result["answer_json_valid"] is True and result["sources_used"] == ["kadry/regulamin.txt"]
    assert result["sources_cited"] == ["kadry/regulamin.txt"] and result["plan_tools"] == ["rag_search"]
    assert result["llm_calls"] == 4  # planner, query rewrite, synthesis, formatter

    # the agent saw the format, as a user would type it
    planner_prompt = llm.prompts_for("task_planner")[0]
    assert "Oczekiwany format odpowiedzi (JSON):" in planner_prompt and "- odpowiedz: liczba dni" in planner_prompt
    # temperature and seed are fixed for every role
    assert {(spec.temperature, spec.seed) for _role, spec in llm.specs} == {(0.0, 42)}

    events = [json.loads(line) for line in (out / "traces" / "T1.jsonl").read_text().splitlines()]
    types = [e["type"] for e in events]
    assert types[0] == "task_start" and types[-1] == "task_end"
    assert {"plan", "node_start", "node_end", "llm_call", "retrieval", "tool_call", "answer_format"} <= set(types)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    retrieval = next(e for e in events if e["type"] == "retrieval")
    assert retrieval["chunks"][0]["text"] == DOC and retrieval["chunks"][0]["file"] == "kadry/regulamin.txt"
    call = next(e for e in events if e["type"] == "llm_call")
    blob = json.loads((out / "blobs" / f"{call['messages_ref'].split(':')[1]}.json").read_text())
    assert blob["messages"][0]["role"] == "system" and call["node"] == "task_planner" and call["params"]["seed"] == 42
    tool = next(e for e in events if e["type"] == "tool_call")
    assert tool["name"] == "rag_search" and tool["depth"] == 0

    run_meta = json.loads((out / "run.json").read_text())
    assert run_meta["tasks"] == {"total": 1, "ok": 1, "error": 0, "timeout": 0}
    assert run_meta["config"]["config_hash"].startswith("sha256:") and run_meta["hapilot"]["commit"]
    assert validate_run(out) == []
    assert (out / "egress.jsonl").read_text() == ""


def test_python_task_with_xlsx_attachment(corpus, tmp_path, llm, docker):
    sheet = tmp_path / "rejestr.xlsx"
    _xlsx(sheet)
    llm.on("task_planner", plan(("python", "zsumuj kwoty z rejestr.xlsx")))
    llm.on("coder", "```python\nimport csv\nprint(1500 + 320)\n```")
    llm.on("answer_formatter", '{"suma": 1820}')
    task = Task(id="T2", instruction="Zsumuj kwoty z załączonego rejestru.", answer_format={"fields": {"suma": "liczba"}},
                attachments=[sheet])
    with EvalSession(_cfg(corpus, tmp_path)) as run:
        result = run.run_task(task)
    assert result["status"] == "ok" and result["answer_json"] == {"suma": 1820}
    assert "rejestr.xlsx" in result["sources_used"]
    assert (tmp_path / "out" / "workspaces" / "T2" / "rejestr.xlsx").is_file()
    assert any(f["path"].startswith("snippet_") for f in result["files_created"])
    assert not any(f["path"] == "rejestr.xlsx" for f in result["files_created"])
    message = llm.prompts_for("task_planner")[0]
    assert "[Załącznik: rejestr.xlsx]" in message and "R-1 | 1500" in message
    assert validate_run(tmp_path / "out") == []


def test_timeout_resume_and_retry(corpus, tmp_path, llm, docker):
    llm.on("task_planner", plan(("chat", "")))
    llm.on("chat", "odpowiedź")
    tasks = [Task(id="A", instruction="Cześć"), Task(id="B", instruction="Hej")]
    with EvalSession(_cfg(corpus, tmp_path, task_timeout_s=1e-9)) as run:
        statuses = [run.run_task(t)["status"] for t in tasks]
    assert statuses == ["timeout", "timeout"]

    with pytest.raises(ConfigError):  # same directory, different configuration
        EvalSession(_cfg(corpus, tmp_path)).start()

    with EvalSession(_cfg(corpus, tmp_path, task_timeout_s=1e-9)) as run:  # resume: nothing re-run
        assert [run.run_task(t)["status"] for t in tasks] == ["timeout", "timeout"]
    assert llm.calls == []
    lines = (tmp_path / "out" / "results.jsonl").read_text().splitlines()
    assert [json.loads(line)["task_id"] for line in lines] == ["A", "B"]
    assert validate_run(tmp_path / "out") == []


def test_graph_error_is_reported_and_run_continues(corpus, tmp_path, llm, docker, monkeypatch):
    llm.on("task_planner", plan(("chat", "")))
    llm.on("chat", "ok")
    with EvalSession(_cfg(corpus, tmp_path)) as run:
        original = run.app

        class Exploding:
            def invoke(self, *a, **k):
                raise RuntimeError("boom")

        run.app = Exploding()
        bad = run.run_task(Task(id="X", instruction="?"))
        run.app = original
        good = run.run_task(Task(id="Y", instruction="Hej"))
    assert bad["status"] == "error" and "boom" in bad["error"] and good["status"] == "ok"


def test_formatter_retries_once_and_parse_mode(llm):
    fmt = {"fields": {"a": "x", "b": "y"}}
    llm.on("answer_formatter", "Oto: {\"a\": 1}", "```json\n{\"a\": 1, \"b\": 2}\n```")
    out = formatter.produce("polecenie", fmt, "odpowiedź", "", "formatter")
    assert out["answer_json_valid"] is True and out["attempts"] == 2 and out["answer_json"] == {"a": 1, "b": 2}
    parsed = formatter.produce("polecenie", fmt, 'Wynik: {"a": 1, "b": [2]} koniec', "", "parse")
    assert parsed["answer_json_valid"] is True and parsed["attempts"] == 0
    bad = formatter.produce("polecenie", {"fields": {"a": "x"}, "schema": {"type": "object", "properties": {"a": {"type": "integer"}}}},
                            '{"a": "nie liczba"}', "", "parse")
    assert bad["answer_json_valid"] is False and bad["answer_json_error"].startswith("schema:")


def test_task_loading_and_validation(tmp_path):
    (tmp_path / "att.txt").write_text("x")
    good = {"id": "T-1", "instruction": "Cześć", "answer_format": {"fields": {"a": "b"}}, "attachments": ["att.txt"]}
    path = tmp_path / "tasks.jsonl"
    path.write_text(json.dumps(good, ensure_ascii=False) + "\n")
    assert load_tasks(path)[0].attachments[0].name == "att.txt"
    for bad in ({"id": "../x", "instruction": "a"}, {"id": "T", "instruction": ""}, {"id": "T", "instruction": "a", "x": 1},
                {"id": "T", "instruction": "a", "answer_format": {"fields": {}}}, {"id": "T", "instruction": "a", "attachments": ["nope"]}):
        path.write_text(json.dumps(bad) + "\n")
        with pytest.raises(ConfigError):
            load_tasks(path)


def test_run_config_rejects_bad_settings(tmp_path):
    for kw in ({"today": "14.10.2026"}, {"approval_policy": "ask"}, {"answer_mode": "guess"}, {"profile": "cloud"}):
        with pytest.raises(ConfigError):
            RunConfig(corpus_dir=tmp_path, out_dir=tmp_path / "o", **{"today": "2026-10-14", **kw})


def test_files_read_by_code_count_as_sources(corpus, tmp_path, llm, docker):
    llm.on("task_planner", plan(("python", "policz z pliku")))
    llm.on("coder", "```python\nprint(open('/data/kadry/regulamin.txt').read()[:5] if False else 14)\n```")
    llm.on("answer_formatter", '{"dni": 14}')
    with EvalSession(_cfg(corpus, tmp_path)) as run:
        result = run.run_task(Task(id="C1", instruction="Ile dni?", answer_format={"fields": {"dni": "liczba"}}))
    assert result["sources_used"] == ["kadry/regulamin.txt"]
    events = [json.loads(line) for line in (tmp_path / "out" / "traces" / "C1.jsonl").read_text().splitlines()]
    assert any(e["type"] == "code_data_access" and e["files"] == ["kadry/regulamin.txt"] for e in events)
    run_volumes = docker.runs[-1]["volumes"]
    assert {"bind": "/data", "mode": "ro"} in run_volumes.values()
    assert validate_run(tmp_path / "out") == []


# --------------------------------------------------------------------------- command line (python -m src.eval)

def test_cli_run_only_and_validate(corpus, tmp_path, llm, docker, capsys):
    from src.eval.__main__ import main

    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text("\n".join(json.dumps({"id": i, "instruction": "Ile dni wcześniej składa się wniosek urlopowy?"})
                               for i in ("T1", "T2")) + "\n", encoding="utf-8")
    run_yaml = tmp_path / "run.yaml"
    run_yaml.write_text(f"corpus_dir: {corpus}\ntoday: '2026-01-01'\nseed: 7\n", encoding="utf-8")
    llm.on("task_planner", plan(("chat", "odpowiedz")))
    llm.on("chat", "14 dni")
    out = tmp_path / "cli_out"
    code = main(["run", "--tasks", str(tasks), "--config", str(run_yaml), "--today", "2026-10-14", "--out", str(out),
                 "--only", "T2"])
    assert code == 0
    meta = json.loads((out / "run.json").read_text())
    assert meta["tasks"]["total"] == 1 and meta["config"]["today"] == "2026-10-14" and meta["config"]["seed"] == 7  # flags win
    assert [json.loads(line)["task_id"] for line in (out / "results.jsonl").read_text().splitlines()] == ["T2"]

    assert main(["validate", str(out)]) == 0 and "VALID" in capsys.readouterr().out
    (out / "results.jsonl").write_text('{"task_id": "T2"}\n')
    assert main(["validate", str(out)]) == 1 and "INVALID" in capsys.readouterr().out


def test_cli_reports_configuration_errors(tmp_path, capsys):
    from src.eval.__main__ import main

    tasks = tmp_path / "tasks.jsonl"
    tasks.write_text('{"id": "T1", "instruction": "x"}\n', encoding="utf-8")
    assert main(["run", "--tasks", str(tasks), "--out", str(tmp_path / "o")]) == 2
    assert "missing settings: corpus_dir, today" in capsys.readouterr().err
    assert main(["run", "--tasks", str(tmp_path / "none.jsonl"), "--corpus", str(tmp_path), "--out", str(tmp_path / "o"),
                 "--today", "2026-10-14"]) == 2
