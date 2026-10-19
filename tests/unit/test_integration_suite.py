"""The integration suite's own logic: scenario files, planner check, checkers, baseline comparison, report."""

import json
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import dev_eval_check  # noqa: E402
import integration  # noqa: E402
import planner_check  # noqa: E402
import smoke  # noqa: E402


def plan(*tools):
    return json.dumps({"task_name": "t", "complexity": "low",
                       "steps": [{"id": i + 1, "action": t, "tool": t, "input": t} for i, t in enumerate(tools)]})


# --------------------------------------------------------------------------- scenario files

def test_planner_scenarios_are_valid_and_use_loaded_tools():
    from src.agent_graph import _build_dynamic_tool_map

    scenarios = planner_check.load_scenarios()
    assert len(scenarios) >= 15
    assert planner_check.validate_scenarios(scenarios, set(_build_dynamic_tool_map())) == []


def test_smoke_scenarios_have_unique_ids_and_checks():
    scenarios = yaml.safe_load((ROOT / "tests" / "scenarios" / "smoke.yaml").read_text(encoding="utf-8"))["scenarios"]
    ids = [sc["id"] for sc in scenarios]
    assert len(ids) == len(set(ids)) and all(sc.get("prompt") and sc.get("checks") for sc in scenarios)


def test_baseline_names_existing_profiles_suites_and_items():
    from src.config import get_settings

    baseline = integration.load_baseline()
    assert set(baseline) <= set(get_settings().profiles)
    dev_ids = set(json.loads(integration.DEV_EXPECTED.read_text(encoding="utf-8")))
    planner_ids = {sc["id"] for sc in planner_check.load_scenarios()}
    for suites in baseline.values():
        assert set(suites) <= set(integration.SUITES)
        assert suites.get("dev", set()) <= dev_ids and suites.get("planner", set()) <= planner_ids


def test_validate_scenarios_reports_problems():
    problems = planner_check.validate_scenarios(
        [{"id": "a", "prompt": "x", "require": ["rag_search"]},
         {"id": "a", "prompt": "y", "forbid": ["teleport"]},
         {"id": "b", "prompt": "z"},
         {"prompt": "w", "order": ["chat"]}], {"rag_search", "chat"})
    assert "a: duplicate id" in problems and "a: unknown tool 'teleport'" in problems
    assert "b: no checks" in problems and "#4: needs id and prompt" in problems and "#4: order needs exactly two tools" in problems


# --------------------------------------------------------------------------- planner check

@pytest.mark.parametrize("tools, sc, failed", [
    (["rag_search", "llm_synthesis"], {"require": ["rag_search"], "forbid": ["template_pdf"]}, []),
    (["llm_synthesis"], {"require": ["rag_search"]}, ["missing rag_search"]),
    (["chat"], {"require_any": [["python", "rag_search"]]}, ["none of ['python', 'rag_search']"]),
    (["llm_synthesis", "template_pdf"], {"forbid": ["template_pdf"]}, ["unexpected template_pdf"]),
    (["template_pdf", "rag_search"], {"order": ["rag_search", "template_pdf"]}, ["template_pdf before rag_search"]),
    (["rag_search"], {"order": ["rag_search", "template_pdf"]}, ["order rag_search → template_pdf needs both"]),
    (["rag_search", "python", "template_pdf"], {"order": ["rag_search", "template_pdf"]}, []),
])
def test_check_plan(tools, sc, failed):
    assert planner_check.check_plan(tools, sc) == failed


def test_planner_run_counts_every_repeat_and_survives_a_crash():
    answers = {"ok": [["rag_search"], ["rag_search"]], "flaky": [["rag_search"], ["chat"]]}

    def fake_plan(sc, profile, session, trace_dir=None):
        if sc["id"] == "crash":
            raise RuntimeError("model gone")
        return answers[sc["id"]].pop(0), []

    scenarios = [{"id": i, "prompt": "?", "require": ["rag_search"]} for i in ("ok", "flaky", "crash")]
    results = {r["id"]: r for r in planner_check.run(scenarios, "best-local", repeat=2, plan=fake_plan)}
    assert results["ok"]["ok"] and not results["flaky"]["ok"] and not results["crash"]["ok"]
    assert results["flaky"]["runs"][1]["failed"] == ["missing rag_search"]
    assert results["crash"]["runs"][0]["failed"] == ["RuntimeError: model gone"]


def test_plan_once_applies_guards_and_writes_traces(llm, tmp_path):
    llm.on("task_planner", plan("llm_synthesis", "template_pdf"))
    tools, guards = planner_check.plan_once({"id": "mail", "prompt": "Napisz maila do zespołu, bez PDF."}, "best-local",
                                            "pc-1", trace_dir=tmp_path)
    assert tools == ["llm_synthesis"] and guards == ["removed_template_pdf:1"]
    events = [json.loads(line) for line in (tmp_path / "pc-1.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {"llm_call", "plan"} <= {e["type"] for e in events}


def test_plan_once_with_document_forces_modify(llm):
    llm.on("task_planner", plan("chat"))
    tools, guards = planner_check.plan_once({"id": "edit", "prompt": "Zmień w tym podaniu daty urlopu na 10–14 listopada.",
                                             "has_document": True}, "best-local", "pc-2")
    assert tools == ["modify_document"] and guards == ["forced_modify_document"]
    assert "podanie_urlop" in llm.prompts_for("task_planner")[0]  # the earlier turn is in the planner's history


# --------------------------------------------------------------------------- checkers of the smoke and development runs

def test_smoke_check(tmp_path):
    (tmp_path / "letter_1.pdf").write_bytes(b"%PDF")
    assert smoke.check("Wynik: 14 dni", {"regex": [r"\b14\b"], "not_contains": ["BŁĄD"]}, tmp_path) == []
    assert smoke.check("krótko", {"min_chars": 40, "contains": ["338350"]}, tmp_path) == [
        "missing '338350'", "answer shorter than 40 chars"]
    assert smoke.check("SUKCES Wygenerowano plik: letter_1.pdf", {"pdf": True}, tmp_path) == []
    assert smoke.check("SUKCES Wygenerowano plik: letter_2.pdf", {"pdf": True}, tmp_path) == ["no PDF generated"]


def _result(**over):
    base = {"status": "ok", "error": None, "answer_json": {}, "sources_used": [], "plan_tools": [], "files_created": []}
    return {**base, **over}


def test_dev_eval_check_rules():
    exp = {"json": {"kwota": {"number": 430.5}, "kto": {"contains": "nowak"}, "zgodne": {"bool": False}},
           "sources_used": ["cennik.pdf"], "plan_tools": ["rag_search"], "files_created_suffix": ".pdf"}
    good = _result(answer_json={"kwota": "430,50 zł", "kto": "Anna Nowak", "zgodne": "false"},
                   sources_used=["docs/cennik.pdf"], plan_tools=["rag_search", "python"], files_created=[{"path": "a/b.pdf"}])
    assert dev_eval_check.check(good, exp) == []
    bad = _result(answer_json={"kwota": True, "kto": "Jan", "zgodne": True})
    failed = dev_eval_check.check(bad, exp)
    assert len(failed) == 6 and failed[0].startswith("kwota=True")
    assert dev_eval_check.check(_result(status="timeout", error="budget"), exp) == ["status timeout: budget"]


# --------------------------------------------------------------------------- baseline comparison and report

def test_compare_classifies_failures():
    c = integration.compare({"D01": True, "D03": False, "D05": True, "D07": False}, {"D03", "D05"})
    assert c == {"passed": 2, "total": 4, "regressions": ["D07"], "known_failures": ["D03"], "now_passing": ["D05"]}


def test_suite_fails_on_regression_invalid_run_error_or_egress():
    clean = {"comparison": integration.compare({"a": True}, set())}
    assert integration.suite_ok(clean)
    assert not integration.suite_ok({"comparison": integration.compare({"a": False}, set())})
    assert integration.suite_ok({"comparison": integration.compare({"a": False}, {"a"})})
    assert not integration.suite_ok({**clean, "valid": False})
    assert not integration.suite_ok({**clean, "error": "no report"})
    assert not integration.suite_ok({**clean, "egress_attempts": ["example.com:443"]})


def test_run_dev_reads_results_and_compares(tmp_path, monkeypatch):
    expected = json.loads(integration.DEV_EXPECTED.read_text(encoding="utf-8"))

    def fake_run(cmd, log, env=None):
        if "run" in cmd:
            out = Path(cmd[cmd.index("--out") + 1])
            out.mkdir(parents=True)
            rows = [_result(task_id=tid, duration_s=1.0, status="ok" if tid != "D03" else "error", error="x")
                    for tid in expected if tid != "D10"]
            (out / "results.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            (out / "egress.jsonl").write_text("")
        return 0

    monkeypatch.setattr(integration, "_run", fake_run)
    monkeypatch.setattr(dev_eval_check, "check", lambda result, exp: [] if result["status"] == "ok" else ["wrong"])
    r = integration.run_dev("reference", tmp_path, Path(sys.executable), "http://127.0.0.1:1/v1", {"D03"})
    assert r["valid"] and r["egress_attempts"] == [] and r["seconds"] == 9.0
    assert r["comparison"]["known_failures"] == ["D03"] and r["comparison"]["regressions"] == ["D10"]
    assert r["failures"]["D10"] == ["not run"]


def test_missing_report_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(integration, "_run", lambda cmd, log, env=None: 3)
    r = integration.run_smoke("best-local", tmp_path, Path(sys.executable), "http://127.0.0.1:1/v1", set())
    assert r["error"] == "no report (exit 3)" and not integration.suite_ok(r)


def test_markdown_report_lists_results_and_failures():
    report = {"date": "2026-10-19T12:00:00", "git": {"describe": "v0-1-gabc", "commit": "abc123def4567890", "dirty": False},
              "unit": {"ok": True, "summary": "150 passed in 4s"}, "passed": False,
              "profiles": {"reference": {"models": ["m"],
                                         "dev": {"comparison": integration.compare({"D03": False, "D07": False, "D05": True},
                                                                                   {"D03", "D05"}),
                                                 "valid": True, "seconds": 12.5, "egress_attempts": [],
                                                 "failures": {"D03": ["kara_zl=330"], "D07": ["no .pdf file created"]}}}}}
    text = integration.render_markdown(report)
    assert "| reference | dev | 1/3 | D03 | D07 | yes | 0 | 12.5 |" in text
    assert "**Result: FAILED**" in text and "reference / dev / D07: no .pdf file created" in text
    assert "reference / dev: D05" in text  # a known failure passed: the baseline can be tightened


def test_smoke_file_checks(tmp_path):
    from docx import Document

    doc = Document()
    doc.add_paragraph("Termin płatności: 30 dni. Wykonawca.")
    doc.save(tmp_path / "umowa_zmieniony.docx")
    answer = "WYNIK:\nSUKCES! Wygenerowano plik: umowa_zmieniony.docx (100 bajtów)"
    ok = {"file": ".docx", "file_contains": ["30 dni"], "file_not_contains": ["Wykonwaca"]}
    assert smoke.check(answer, ok, tmp_path) == []
    assert smoke.check(answer, {"file": ".docx", "file_contains": ["14 dni"], "file_not_contains": ["Wykonawca"]},
                       tmp_path) == ["umowa_zmieniony.docx lacks '14 dni'", "umowa_zmieniony.docx still has 'Wykonawca'"]
    assert smoke.check(answer, {"file": ".txt"}, tmp_path) == ["no .txt file generated"]


def test_smoke_scenario_attachments_exist():
    scenarios = yaml.safe_load((ROOT / "tests" / "scenarios" / "smoke.yaml").read_text(encoding="utf-8"))["scenarios"]
    attached = [a for sc in scenarios for a in sc.get("attachments", [])]
    assert attached and all((ROOT / a).is_file() for a in attached)


def test_planner_scenario_with_attachments_lists_them(llm):
    llm.on("task_planner", plan("file_summary"))
    tools, _ = planner_check.plan_once({"id": "f", "prompt": "Streść to.", "attachments": ["raport.pdf"]}, "best-local", "pc-3")
    assert tools == ["file_summary"]
    assert "Załączone pliki (dostępne też w katalogu roboczym dla kodu Python): raport.pdf" in llm.prompts_for("task_planner")[0]
