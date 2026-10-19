"""Graph runner (regression: the UI waited forever when the graph raised), model profiles, login, n8n."""

import asyncio
import json
import re
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from src import llm_config, run_context
from src.graph_runner import ERROR_PREFIX, final_text, generated_pdf_name, run_graph_streaming

ROOT = Path(__file__).resolve().parents[2]


class _ExplodingApp:
    def stream(self, inputs, config=None, stream_mode=None):
        yield {"task_planner": {"task_plan": {}}}
        raise RuntimeError("model server went away")


class _OkApp:
    def stream(self, inputs, config=None, stream_mode=None):
        yield {"chat": {"messages": [AIMessage(content="cześć")]}}


def test_runner_finishes_and_reports_when_graph_raises():
    seen = []

    async def on_event(name, output):
        seen.append(name)

    run = asyncio.run(asyncio.wait_for(run_graph_streaming(_ExplodingApp(), {}, {}, on_event), timeout=5))
    assert seen == ["task_planner"]
    assert run.error and "model server went away" in run.error
    assert final_text(run).startswith(ERROR_PREFIX)


def test_runner_survives_a_failing_ui_callback():
    async def on_event(name, output):
        raise ValueError("UI broke")

    run = asyncio.run(asyncio.wait_for(run_graph_streaming(_OkApp(), {}, {}, on_event), timeout=5))
    assert run.error is None and final_text(run) == "cześć"


def test_generated_pdf_name():
    assert generated_pdf_name("SUKCES! Wygenerowano plik: letter_1.pdf (10 bajtów)") == "letter_1.pdf"
    assert generated_pdf_name("nic") is None


# --------------------------------------------------------------------------- profiles and fallback

def test_role_model_comes_from_session_profile(llm):
    with run_context.use_context(profile="best-local"):
        llm_config.get_llm("writer")
    with run_context.use_context(profile="reference"):
        llm_config.get_llm("writer")
        llm_config.get_llm("coder")
    models = [spec.model for _role, spec in llm.specs]
    assert models == ["qwen3.6-27b-q4km", "bielik-11b-v3-q8", "qwen3-coder-30b-a3b-q4km"]


def test_coder_falls_back_to_default_model_and_fallback_prompt(llm, monkeypatch):
    monkeypatch.setattr(llm_config, "model_available", lambda spec, ttl=30.0: False)
    with run_context.use_context(profile="reference"):
        _model, prompt = llm_config.get_llm("coder")
    assert llm.specs[-1][1].model == "bielik-11b-v3-q8"
    from src.core.prompt_registry import prompt_registry

    if prompt_registry.has("coder_fallback"):
        assert prompt.startswith(prompt_registry.get("coder_fallback")[:40])


def test_real_factory_rejects_non_local_endpoint():
    from src.config import ModelSpec, NotLocalEndpoint

    with pytest.raises(NotLocalEndpoint):
        llm_config._openai_compatible(ModelSpec(key="x", base_url="https://api.openai.com/v1", model="gpt"), "chat")


def test_prompt_contains_run_date(llm):
    with run_context.use_context(today="2026-10-14"):
        _m, prompt = llm_config.get_llm("chat")
    assert "14 października 2026" in prompt


# --------------------------------------------------------------------------- UI login

def test_password_hash_roundtrip(tmp_path):
    from src.ui_auth import check_password, hash_password, verify_password

    encoded = hash_password("tajne-hasło")
    assert verify_password("tajne-hasło", encoded) and not verify_password("inne", encoded)
    users = tmp_path / "users.txt"
    users.write_text(f"anna:{encoded}\n", encoding="utf-8")
    assert check_password("anna", "tajne-hasło", users)
    assert not check_password("jan", "tajne-hasło", users)


# --------------------------------------------------------------------------- n8n (optional, off by default)

def _n8n():
    import importlib.util

    spec = importlib.util.spec_from_file_location("n8n_nodes", ROOT / "extensions" / "n8n_operations" / "nodes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_n8n_extension_is_off_by_default():
    manifest = json.loads((ROOT / "extensions" / "n8n_operations" / "hapilot_extension.json").read_text())
    assert manifest["enabled"] is False
    from src.agent_tools import available_planner_tools

    tools = available_planner_tools()
    assert "email" not in tools and "calendar" not in tools and "rag_search" in tools


def test_n8n_date_parsing():
    from datetime import date

    n8n = _n8n()
    today = date(2026, 10, 14)
    assert n8n.parse_date("spotkania jutro", today) == date(2026, 10, 15)
    assert n8n.parse_date("na 20.10.2026", today) == date(2026, 10, 20)
    assert n8n.parse_date("2026-11-02", today) == date(2026, 11, 2)
    assert n8n.parse_date("dzisiaj", today) == today


def test_n8n_email_draft_needs_confirmation(llm, monkeypatch):
    n8n = _n8n()
    posted = []
    monkeypatch.setattr(n8n.httpx, "post", lambda url, json=None, timeout=None: posted.append((url, json)))
    llm.on("writer", '{"to": "anna@example.com", "subject": "Termin", "body": "Treść"}')
    state = {"messages": [], "task_plan": {"steps": [{"tool": "email", "input": "Napisz do Anny"}]}, "current_step": 0}

    with run_context.use_context(approval_policy="auto"):
        out = n8n.run_operations(state)
    assert posted == [] and "nie utworzono" in out["messages"][0].content

    with run_context.use_context(approval_policy="ask"):
        out = n8n.run_operations(state)
    cont = out["hitl_request"]["continuation"]
    assert posted == [] and cont["resume_node"] == "operations" and cont["draft"]["subject"] == "Termin"


# --------------------------------------------------------------------------- static privacy checks on the repository

SOURCE_FILES = [p for p in (ROOT / "src").rglob("*.py")] + [p for p in (ROOT / "extensions").rglob("*.py")]


def test_no_cloud_code_paths():
    for path in SOURCE_FILES:
        text = path.read_text(encoding="utf-8")
        assert "USE_OPENAI" not in text, path
        assert not re.search(r"api\.openai\.com|api\.anthropic\.com|e2b", text), path


def test_requirements_have_no_cloud_sandbox():
    for name in ("requirements.txt", "requirements-test.txt"):
        path = ROOT / name
        if path.is_file():
            assert "e2b" not in path.read_text().lower(), name


def test_compose_ports_are_bound_to_localhost():
    compose = (ROOT / "docker" / "docker-compose.yml").read_text()
    for line in compose.splitlines():
        if re.match(r"\s*-\s*\"?[\d.:]*\d+:\d+", line):
            assert "127.0.0.1:" in line, line


def test_display_text_hides_tool_markers():
    from src.graph_runner import display_text

    assert display_text("WYNIK:\nSUKCES! Wygenerowano plik: letter_1.pdf (1234 bajtów)") == "📄 Gotowe: **letter_1.pdf**"
    assert display_text("WYNIK:\nSUKCES:\n338350\n") == "🐍 **Wynik programu:**\n\n```text\n338350\n```"
    assert display_text("WYNIK:\nSUKCES:\n") .endswith("(program nic nie wypisał)\n```")
    assert display_text("WYNIK:\nBŁĄD: Nie zmieniłem pliku a.txt: x") == "⚠️ BŁĄD: Nie zmieniłem pliku a.txt: x"
    edited = display_text("WYNIK:\nSUKCES! Wygenerowano plik: umowa_zmieniony.docx (99 bajtów)\nNowa wersja pliku umowa.docx.")
    assert edited == "📄 Gotowe: **umowa_zmieniony.docx**\nNowa wersja pliku umowa.docx."
    assert display_text("Zwykła odpowiedź o SUKCES-ie firmy.") == "Zwykła odpowiedź o SUKCES-ie firmy."


def test_generated_file_names_and_form_title():
    from src.graph_runner import form_title, generated_file_names

    text = "Wygenerowano plik: a.pdf (1 bajtów) i Wygenerowano plik: b_zmieniony.docx; znowu Wygenerowano plik: a.pdf"
    assert generated_file_names(text) == ["a.pdf", "b_zmieniony.docx"]
    assert generated_file_names("Wygenerowano plik: skrypt.sh") == []
    assert form_title("📋 **Przegląd parametrów dokumentu: List / Podanie**\n\nSprawdź pola.") == \
        "Przegląd parametrów dokumentu: List / Podanie"
    assert form_title("") == "Parametry dokumentu"
