"""v3 stage 0 driver (agent/spike/spike.py): prompt, container flags, egress parsing, rows and scoring summary."""

from __future__ import annotations

import importlib.util
import json
import types
from argparse import Namespace
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("v3_spike", ROOT / "agent" / "spike" / "spike.py")
spike = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(spike)

TEMPLATE = {"system": "Asystent firmy {firma}. Dziś: {data}", "task": "ZADANIE:\n{polecenie}\n\nFORMAT:\n{format}",
            "workspace_note": "\n\nOdpowiedź zapisz w odpowiedz.json."}
TASK = {"id": "T01", "polecenie": "  Ile dni?\n", "format_odpowiedzi": {"dni": "liczba dni", "zrodla": "pliki"},
        "kategoria": "rozumowanie", "poziom": "L2", "sprawdzenia": []}


def test_prompt_fills_company_polish_date_task_and_format():
    p = spike.build_prompt(TEMPLATE, TASK, "Przykładowa Sp. z o.o.", date(2026, 11, 12))
    assert p.startswith("Asystent firmy Przykładowa Sp. z o.o.. Dziś: czwartek, 12 listopada 2026 r.\n\nZADANIE:\nIle dni?")
    assert '"dni": "liczba dni"' in p and p.endswith("Odpowiedź zapisz w odpowiedz.json.")


def test_polish_date_names_weekday_and_genitive_month():
    assert spike.polish_date(date(2026, 12, 24)) == "czwartek, 24 grudnia 2026 r."
    assert spike.polish_date(date(2027, 1, 3)) == "niedziela, 3 stycznia 2027 r."


def _tcp_line(ip_le_hex: str, port: int, state: str) -> str:
    return f"   0: 0100007F:1F40 {ip_le_hex}:{port:04X} {state} 00000000:00000000 00:00000000 00000000  1000"


def test_public_connections_keeps_only_public_established_or_syn_sent():
    header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid"
    text = "\n".join([
        header,
        _tcp_line("0100007F", 8000, "01"),   # 127.0.0.1 loopback
        _tcp_line("020013AC", 8000, "01"),   # 172.19.0.2 private (the relay)
        _tcp_line("01010101", 443, "02"),    # 1.1.1.1 SYN_SENT → reported
        _tcp_line("08080808", 53, "0A"),     # 8.8.8.8 LISTEN → ignored
        "garbage",
    ])
    assert spike.public_connections(text) == {"1.1.1.1:443"}


def test_public_connections_reads_ipv4_mapped_ipv6():
    mapped = "0000000000000000FFFF000001010101"  # ::ffff:1.1.1.1 as /proc prints it (32-bit words, little endian)
    line = f"   0: 00000000000000000000000001000000:1F40 {mapped}:01BB 01 0:0 00:0 0 1000"
    assert spike.public_connections("hdr\n" + line) == {"1.1.1.1:443"}


def test_docker_command_isolates_the_container(tmp_path):
    a = Namespace(network="hapilot-v3-izolowana", allow="model:8000", image="hapilot-agent:0.2")
    cmd = spike.docker_command(a, "hapilot-v3-t01", tmp_path / "p", tmp_path / "d", tmp_path / "r",
                               tmp_path / "k", tmp_path / "t")
    joined = " ".join(cmd)
    for flag in ("--network hapilot-v3-izolowana", "--read-only", "--cap-drop ALL", "no-new-privileges",
                 "--pids-limit", "HAPILOT_EGRESS_ALLOW=model:8000", f"{tmp_path / 'k'}:/praca/dokumenty:ro",
                 f"{tmp_path / 't'}:/praca/tekst:ro", "--import /opt/hapilot-agent/egress_guard.mjs"):
        assert flag in joined, flag
    assert "--privileged" not in joined and "docker.sock" not in joined and "--network host" not in joined


def test_summarize_trace_counts_tools_errors_iterations(tmp_path):
    t = tmp_path / "trace.jsonl"
    t.write_text("\n".join(json.dumps(e) for e in [
        {"type": "tool_end", "tool": "read_files", "error": None},
        {"type": "tool_end", "tool": "run_commands", "error": "exit 1"},
        {"type": "tool_end", "tool": "read_files", "error": None},
        {"type": "iteration", "n": 3},
        {"type": "notice", "reason": "auto_compaction"},
    ]) + "\nnot json\n", encoding="utf-8")
    s = spike.summarize_trace(t)
    assert s == {"tool_calls": {"read_files": 2, "run_commands": 1}, "tool_errors": 1, "iterations": 3,
                 "notices": ["auto_compaction"]}
    assert spike.summarize_trace(tmp_path / "missing.jsonl")["tool_calls"] == {}


def test_make_row_marks_missing_answer_and_keeps_run_details():
    parse = lambda raw: json.loads(raw)  # noqa: E731
    trace = {"tool_calls": {}, "tool_errors": 0, "iterations": 1, "notices": []}
    empty = spike.make_row("T01", "", parse, {"finish_reason": "completed"}, "ok", None, 12.5, 0, trace, 0, 0)
    assert empty["answer_json_valid"] is False and empty["error"] == "no odpowiedz.json (exit 0)"
    ok = spike.make_row("T01", '{"dni": 3}', parse, {"submitted": {"verified": True}}, "ok", None, 1.0, 0, trace, 2, 0)
    assert ok["answer_json"] == {"dni": 3} and ok["submitted"] == {"verified": True}
    assert ok["egress_blocked_node"] == 2 and ok["sources_used"] == []


def test_load_manifest_indexes_documents_by_id(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps({"dokumenty": [
        {"id": "D1", "sciezka": "a/umowa.pdf"}, {"id": "D2", "sciezka": "b/notatka.txt"}]}), encoding="utf-8")
    assert spike.load_manifest(tmp_path) == {"D1": {"id": "D1", "sciezka": "a/umowa.pdf"},
                                             "D2": {"id": "D2", "sciezka": "b/notatka.txt"}}


def test_score_rows_uses_the_task_sets_scorer_and_counts_categories():
    calls = []

    def score(task, raw, idx):
        calls.append((task["id"], raw, idx))
        good = raw == '{"ok": 1}'
        return {"sukces": good, "wynik": 1.0 if good else 0.25, "json_poprawny": bool(raw),
                "sprawdzenia": [{"id": "c1", "ok": good}]}

    scoring = types.SimpleNamespace(score=score)
    tasks = [dict(TASK, id="T01"), dict(TASK, id="T02", kategoria="pisma"), dict(TASK, id="T03")]
    rows = {"T01": {"answer_text": '{"ok": 1}', "status": "ok"}, "T02": {"answer_text": "x", "status": "ok"}}
    graded, summary = spike.score_rows(tasks, rows, scoring, {"DOC": {}})
    assert [g["sukces"] for g in graded] == [True, False, False]
    assert graded[2]["status"] == "missing" and graded[1]["nieudane"] == ["c1"]
    assert summary == {"tasks": 3, "success": 1, "mean_score": 0.5, "valid_json": 2,
                       "by_category": {"pisma": "0/1", "rozumowanie": "1/2"}}
    assert calls[0][2] == {"DOC": {}}
