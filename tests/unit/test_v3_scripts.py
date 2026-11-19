"""The v3 integration scripts without Docker or a model: the smoke checks and the container egress test."""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import egress_v3  # noqa: E402
import smoke_v3  # noqa: E402

# --------------------------------------------------------------------------- smoke_v3


def test_smoke_v3_checks_answer_and_files(tmp_path):
    from docx import Document

    doc = Document()
    doc.add_paragraph("Termin płatności: 30 dni.")
    doc.save(tmp_path / "umowa.docx")
    files = [{"name": "umowa.docx", "path": str(tmp_path / "umowa.docx")}, {"name": "pismo.pdf", "path": "x"}]
    assert smoke_v3.check("Wynik: 14 dni", {"regex": [r"\b14\b"], "contains": ["dni"], "not_contains": ["BŁĄD"]}, []) == []
    assert smoke_v3.check("krótko", {"min_chars": 40, "contains": ["338350"]}, []) == [
        "missing '338350'", "answer shorter than 40 chars"]
    assert smoke_v3.check("ok", {"pdf": True, "file": ".docx", "file_contains": ["30 dni"]}, files) == []
    assert smoke_v3.check("ok", {"pdf": True, "file": ".xlsx"}, files[:1]) == ["no PDF generated", "no .xlsx file generated"]
    assert smoke_v3.check("ok", {"file": ".docx", "file_not_contains": ["30 dni"]}, files) == [
        "umowa.docx still has '30 dni'"]


def test_smoke_v3_refused_lists_blocked_connections(tmp_path):
    assert smoke_v3.refused(tmp_path) == []
    (tmp_path / "egress.jsonl").write_text("\n".join([
        json.dumps({"host": "model", "port": 8000, "allowed": True}),
        "not json",
        json.dumps({"host": "example.com", "port": 443, "allowed": False})]))
    assert smoke_v3.refused(tmp_path) == ["agent container: example.com:443"]


def test_headless_ui_answers_first_option_and_refuses():
    import asyncio

    ui = smoke_v3.HeadlessUI()
    assert asyncio.run(ui.ask("?", ["tak", "nie"])) == "tak"
    assert asyncio.run(ui.ask("?", [])).startswith("(brak odpowiedzi")
    assert asyncio.run(ui.approve("send_email", {})) == (False, "brak zgody (test bez użytkownika)")
    assert ui.asked == 2 and ui.approvals == 1


# --------------------------------------------------------------------------- egress_v3


def _settings():
    import dataclasses

    from src.config import get_settings

    return dataclasses.replace(get_settings(), agent_model="m", agent_email=False)


def test_probe_runs_with_the_agents_own_flags():
    from src.agent_engine import RUNNER

    s = _settings()
    cmd, work = egress_v3.probe_command(s, ["python3", "-c", "print(1)"])
    try:
        image_at = cmd.index(s.agent_image)
        assert cmd[image_at + 1:] == ["python3", "-c", "print(1)"]
        flags = cmd[:image_at]
        assert flags[flags.index("--network") + 1] == s.agent_network
        assert {"--read-only", "--cap-drop", "ALL", "no-new-privileges"} <= set(flags)
        assert f"HAPILOT_EGRESS_ALLOW={s.agent_allow}" in flags and f"{RUNNER}/run_task.mjs" not in cmd
        assert (work / "run").is_dir() and (work / "dom").is_dir()
    finally:
        import shutil

        shutil.rmtree(work)


def _fake_docker(model_ids, node_verdict, refused=True, internal="true"):
    def run(cmd, **kwargs):
        if cmd[:3] == ["docker", "network", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0, internal + "\n", "")
        if "-c" in cmd:  # the Python probe
            out = {"dns example.com": "blocked (gaierror)", "tcp 1.1.1.1:443": "blocked (TimeoutError)",
                   "udp 8.8.8.8:53": "blocked (OSError)", "model": model_ids}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(out) + "\n", "")
        run_dir = next(Path(a.split(",")[1].split("=", 1)[1]) for a in cmd if a.endswith("target=/run"))
        if refused:
            (run_dir / "egress.jsonl").write_text(json.dumps({"host": "1.1.1.1", "port": 80, "allowed": False}) + "\n")
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"node_fetch": node_verdict}) + "\n", "")
    return run


def test_egress_v3_passes_when_nothing_gets_out(tmp_path, monkeypatch):
    import src.eval.agent as eval_agent

    monkeypatch.setattr(eval_agent, "image_id", lambda image: "sha256:abc")
    monkeypatch.setattr(egress_v3.subprocess, "run", _fake_docker(["qwen3.6-27b-q4km"], "blocked: egress blocked: 1.1.1.1:80"))
    report = tmp_path / "egress.json"
    assert egress_v3.main(["--profile", "best-local", "--report", str(report)]) == 0
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["passed"] and data["model"] == "qwen3.6-27b-q4km" and data["image_id"] == "sha256:abc"
    names = [c["check"] for c in data["checks"]]
    assert names[0] == "agent network is internal" and "runner guard logs the refusal" in names
    assert any(n.startswith("model relay") for n in names) and "no way out: tcp 1.1.1.1:443" in names


def test_egress_v3_fails_on_a_leak_or_a_missing_model(tmp_path, monkeypatch):
    import src.eval.agent as eval_agent

    monkeypatch.setattr(eval_agent, "image_id", lambda image: None)
    monkeypatch.setattr(egress_v3.subprocess, "run", _fake_docker(["other-model"], "connected", refused=False,
                                                                  internal="false"))
    report = tmp_path / "egress.json"
    assert egress_v3.main(["--profile", "gemma", "--report", str(report)]) == 1
    failed = {c["check"] for c in json.loads(report.read_text(encoding="utf-8"))["checks"] if not c["ok"]}
    assert failed == {"agent network is internal", "model relay model:8000 serves gemma-4-31b-qat",
                      "runner guard refuses Node connections", "runner guard logs the refusal"}
