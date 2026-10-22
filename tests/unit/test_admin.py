"""Admin panel logic (src/admin.py): admins, prompt overrides with history, custom nodes, the agent graph."""

import json

import pytest

from src import admin
from src.core.prompt_registry import prompt_registry

NODE = {"name": "streszczacz", "label": "🧾 Streszczacz umów",
        "description": "Krótkie streszczenie umowy lub regulaminu w punktach",
        "when_to_use": "Gdy użytkownik prosi o streszczenie umowy", "when_not_to_use": "Gdy chce pliku PDF",
        "prompt": "Jesteś asystentem, który streszcza umowy w pięciu punktach. Pisz po polsku.",
        "model_role": "coder", "temperature": 0.1}


@pytest.fixture
def admin_env(tmp_path, monkeypatch):
    """Custom extensions in a temporary folder; the original graph and manager come back afterwards."""
    import src.core.extension_manager as em
    from src.agent_graph import reset_app

    original = em.extension_manager
    monkeypatch.setenv("HAPILOT_CUSTOM_EXTENSIONS_DIR", str(tmp_path / "custom"))
    monkeypatch.setenv("HAPILOT_EXTENSIONS_DIRS", "")
    admin.enable_for_ui()
    reset_app()
    yield tmp_path / "custom"
    for path in admin.overrides_dir().glob("*.json"):
        prompt_registry.clear_override(path.stem)
        path.unlink()
    if admin.audit_path().exists():
        admin.audit_path().unlink()
    prompt_registry.unregister(NODE["name"])
    em.extension_manager = original
    em.set_active(original)
    reset_app()


def test_admins_from_environment(monkeypatch):
    monkeypatch.delenv("HAPILOT_UI_ADMINS", raising=False)
    assert admin.admins() == {"admin"} and admin.is_admin("admin") and not admin.is_admin("uzytkownik")
    monkeypatch.setenv("HAPILOT_UI_ADMINS", "anna, piotr")
    assert admin.admins() == {"anna", "piotr"} and not admin.is_admin("admin") and not admin.is_admin(None)


def test_prompt_override_history_restore_and_audit(admin_env):
    admin.graph_nodes()  # loads the extensions and their prompts
    default = prompt_registry.get("writer")
    info = admin.save_prompt("writer", "Nowy prompt redaktora: pisz krótko i rzeczowo.", "admin")
    assert info["overridden"] and info["current"].startswith("Nowy prompt") and info["default"] == default
    assert prompt_registry.get("writer").startswith("Nowy prompt") and info["nodes"] == ["writer"]
    admin.save_prompt("writer", "Druga wersja promptu redaktora, jeszcze krótsza.", "anna")
    info = admin.restore_prompt("writer", "admin")
    assert not info["overridden"] and prompt_registry.get("writer") == default
    assert [(h["user"], h["restored"]) for h in info["history"]] == [("admin", True), ("anna", False), ("admin", False)]
    assert [e["action"] for e in admin.audit_log()] == ["prompt_restored", "prompt_saved", "prompt_saved"]
    # the history survives a restart: the latest version is applied again
    admin.save_prompt("writer", "Wersja po restarcie, obowiązująca dalej.", "admin")
    prompt_registry.clear_override("writer")
    assert admin.load_prompt_overrides() == 1 and prompt_registry.get("writer").startswith("Wersja po restarcie")
    listed = {p["role"]: p for p in admin.list_prompts()}
    assert listed["writer"]["overridden"] and listed["writer"]["versions"] == 4 and "task_planner" in listed


@pytest.mark.parametrize("role,text,message", [("nie_ma_takiej", "Dowolny dłuższy tekst promptu.", "Nie ma promptu"),
                                               ("writer", "krótki", "za krótki"), ("../etc", "Dowolny dłuższy tekst.", "Niepoprawna")])
def test_prompt_save_refusals(admin_env, role, text, message):
    admin.graph_nodes()
    with pytest.raises(admin.AdminError, match=message):
        admin.save_prompt(role, text, "admin")


def test_custom_node_is_created_planned_and_answers_with_its_prompt(admin_env, llm, invoke):
    from src.agent_graph import get_app
    from src.core.extension_manager import get_active

    admin.create_custom_node(NODE, "admin")
    folder = admin_env / "streszczacz"
    assert json.loads((folder / "hapilot_extension.json").read_text())["config"]["custom"] is True
    nodes = {n["id"]: n for n in admin.graph_nodes()}
    assert nodes["streszczacz"]["kind"] == "custom" and nodes["streszczacz"]["prompts"] == ["streszczacz"]
    assert nodes["writer"]["kind"] == "extension" and nodes["task_planner"]["kind"] == "core"
    assert get_active().tool_node_map()["streszczacz"] == "streszczacz"

    llm.on("task_planner", '{"steps": [{"id": 1, "tool": "streszczacz", "action": "streść umowę", "input": "umowa serwisowa"}]}')
    llm.on("streszczacz", "1. Umowa na serwis. 2. Rok. 3. Płatność miesięczna. 4. SLA 8h. 5. Wypowiedzenie 1 mies.")
    state = invoke(get_app(), "Streść umowę serwisową w punktach", "s-custom", profile="reference")
    assert state["messages"][-1].content.startswith("1. Umowa na serwis")
    assert "Krótkie streszczenie umowy" in llm.prompts_for("task_planner")[0]  # the planner sees the new tool
    prompt = llm.prompts_for("streszczacz")[0]
    assert "streszcza umowy w pięciu punktach" in prompt and "Streść umowę serwisową" in prompt
    role, spec = next((r, s) for r, s in llm.specs if r == "streszczacz")
    assert "coder" in spec.model and spec.temperature == 0.1  # model_role "coder" of the profile, own temperature
    assert admin.audit_log()[0]["action"] == "node_created"


@pytest.mark.parametrize("change,message", [({"name": "writer"}, "zajęta"), ({"name": "Zła Nazwa"}, "Nazwa techniczna"),
                                            ({"prompt": "za krótko"}, "Prompt systemowy"), ({"model_role": "gpt"}, "Model"),
                                            ({"temperature": 3}, "Temperatura"), ({"label": ""}, "Etykieta")])
def test_invalid_custom_nodes_are_refused_and_leave_nothing(admin_env, change, message):
    with pytest.raises(admin.AdminError, match=message):
        admin.create_custom_node({**NODE, **change}, "admin")
    assert not any(admin_env.iterdir()) if admin_env.exists() else True


def test_custom_node_can_be_deleted_builtin_cannot(admin_env):
    admin.create_custom_node(NODE, "admin")
    admin.save_prompt("streszczacz", "Zmieniony prompt streszczacza umów.", "admin")
    admin.delete_custom_node("streszczacz", "admin")
    assert not (admin_env / "streszczacz").exists() and not (admin.overrides_dir() / "streszczacz.json").exists()
    assert "streszczacz" not in {n["id"] for n in admin.graph_nodes()} and not prompt_registry.has("streszczacz")
    for name in ("writer", "task_planner", "../writer", "streszczacz"):
        with pytest.raises(admin.AdminError):
            admin.delete_custom_node(name, "admin")
    assert [e["action"] for e in admin.audit_log()][:2] == ["node_deleted", "prompt_saved"]


def test_graph_edges_match_the_compiled_graph(admin_env):
    admin.create_custom_node(NODE, "admin")
    data = admin.graph_data()
    compiled = {tuple(e) for e in data["compiled_edges"]}
    concrete = [(e["source"], e["target"]) for e in data["edges"] if "__next__" not in (e["source"], e["target"])]
    assert concrete and set(concrete) <= compiled  # every drawn edge exists in LangGraph
    kinds = {(e["source"], e["target"]): e["kind"] for e in data["edges"]}
    assert kinds[("coder", "execute")] == "then" and kinds[("execute", "coder")] == "retry"
    assert kinds[("router", "streszczacz")] == "route" and kinds[("streszczacz", "__next__")] == "next"
    # every worker that continues with the plan really can go to any node or end in LangGraph
    workers = {n["id"] for n in data["nodes"]} - {"task_planner", "router"}
    for source, target in concrete:
        assert source in workers | {"__start__", "task_planner", "router"}
    assert {("writer", "__end__"), ("writer", "streszczacz"), ("streszczacz", "writer")} <= compiled


def test_evaluation_runs_never_see_custom_nodes(monkeypatch):
    from src.core.extension_manager import default_dirs

    monkeypatch.delenv("HAPILOT_EXTENSIONS_DIRS", raising=False)
    assert all("custom_extensions" not in str(d) for d in default_dirs())
    from src.config import PROJECT_ROOT

    eval_code = "".join(p.read_text(encoding="utf-8") for p in (PROJECT_ROOT / "src" / "eval").glob("*.py"))
    assert "enable_for_ui" not in eval_code and "src.admin" not in eval_code
