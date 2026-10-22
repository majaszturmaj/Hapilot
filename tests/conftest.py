"""Unit-test setup: isolated data directory, scripted LLM, fake Docker. No model, no network, no containers."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def pytest_configure(config):
    import tempfile

    data_dir = Path(tempfile.mkdtemp(prefix="hapilot-tests-"))
    os.environ["HAPILOT_DATA_DIR"] = str(data_dir)
    os.environ["HAPILOT_APPROVAL_POLICY"] = "ask"
    os.environ.pop("HAPILOT_PROFILE", None)
    # pinned, not removed: src/config.py loads .env, which only fills variables that are not set
    os.environ["HAPILOT_PROFILES_FILE"] = str(ROOT / "config" / "profiles.yaml")
    os.environ["HAPILOT_UI_USERS_FILE"] = ""
    os.environ["HAPILOT_CHAT_DB"] = str(data_dir / "chat_history.sqlite")

    # Unit tests never touch the network: any non-local connection or DNS lookup is blocked and fails the test.
    from src.privacy import install_egress_guard

    install_egress_guard(data_dir / "egress_attempts.jsonl")


@pytest.fixture(autouse=True)
def _no_egress(request):
    from src.privacy import attempts

    before = len(attempts())
    yield
    if request.node.get_closest_marker("egress_probe") is None:
        new = attempts()[before:]
        assert not new, f"test attempted network egress: {[a['target'] for a in new]}"


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch):
    from src.config import reset_settings

    reset_settings()
    yield
    reset_settings()


@pytest.fixture
def llm():
    """Scripted LLM installed as the model factory."""
    from fakes import LLMScript
    from src import llm_config

    script = LLMScript()
    llm_config.set_llm_factory(script.factory)
    yield script
    llm_config.set_llm_factory(None)


@pytest.fixture
def docker():
    from fakes import FakeDocker
    from src import sandbox

    fake = FakeDocker()
    sandbox.set_docker_client(fake)
    yield fake
    sandbox.set_docker_client(None)


@pytest.fixture
def session():
    """A unique session id with its context active (for calling tools outside the graph)."""
    from src import run_context

    sid = f"test-{uuid.uuid4().hex[:8]}"
    with run_context.use_context(session_id=sid):
        yield sid


@pytest.fixture
def graph():
    from src.agent_graph import get_app

    return get_app()


def run_graph(app, text_or_messages, session_id, approval_policy="ask", hitl_response=None, last_file="", profile=None):
    """Invoke the graph like the UI does; returns the final state."""
    from langchain_core.messages import HumanMessage

    from src import run_context

    messages = [HumanMessage(content=text_or_messages)] if isinstance(text_or_messages, str) else list(text_or_messages)
    inputs = {"messages": messages, "next_step": "", "execution_status": "", "retry_count": 0, "task_plan": {},
              "task_id": "", "is_complex_task": False, "current_step": None, "generated_code": None,
              "last_generated_file": last_file, "hitl_request": None, "hitl_response": hitl_response, "halt": False}
    config = run_context.make_config(session_id, approval_policy=approval_policy, profile=profile)
    return app.invoke(inputs, config=config)


@pytest.fixture
def invoke():
    return run_graph
