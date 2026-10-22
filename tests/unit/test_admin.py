"""Agent panel logic (src/admin.py): roles, built-in prompt overrides, private and global custom nodes, graphs."""

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
def panel(tmp_path, monkeypatch):
    """Custom nodes in a temporary folder; admins: ``admin``; everything cleaned up afterwards."""
    monkeypatch.setenv("HAPILOT_CUSTOM_EXTENSIONS_DIR", str(tmp_path / "custom"))
    monkeypatch.delenv("HAPILOT_UI_ADMINS", raising=False)
    admin.forget_agents()
    admin.enable_for_ui()
    yield tmp_path / "custom"
    for path in admin.overrides_dir().glob("*.json"):
        prompt_registry.clear_override(path.stem)
        path.unlink()
    if admin.audit_path().exists():
        admin.audit_path().unlink()
    for name in ("streszczacz", "notatnik", "inny"):
        prompt_registry.unregister(name)
    admin.forget_agents()


def ids(user):
    return {n["id"]: n for n in admin.graph_nodes(user)}


def test_admins_from_environment(monkeypatch):
    monkeypatch.delenv("HAPILOT_UI_ADMINS", raising=False)
    assert admin.admins() == {"admin"} and admin.is_admin("admin") and not admin.is_admin("uzytkownik")
    monkeypatch.setenv("HAPILOT_UI_ADMINS", "anna, piotr")
    assert admin.admins() == {"anna", "piotr"} and not admin.is_admin("admin") and not admin.is_admin(None)


# --------------------------------------------------------------------------- built-in prompts

def test_prompt_override_history_restore_and_audit(panel):
    default = admin.prompt_info("writer")["default"]
    info = admin.save_prompt("writer", "Nowy prompt redaktora: pisz krótko i rzeczowo.", "admin")
    assert info["overridden"] and info["current"].startswith("Nowy prompt") and info["default"] == default
    assert prompt_registry.get("writer").startswith("Nowy prompt") and info["nodes"] == ["writer"]
    admin.save_prompt("writer", "Druga wersja promptu redaktora, jeszcze krótsza.", "admin")
    info = admin.restore_prompt("writer", "admin")
    assert not info["overridden"] and prompt_registry.get("writer") == default
    assert [(h["user"], h["restored"]) for h in info["history"]] == [("admin", True), ("admin", False), ("admin", False)]
    assert [e["action"] for e in admin.audit_log("admin")] == ["prompt_restored", "prompt_saved", "prompt_saved"]
    admin.save_prompt("writer", "Wersja po restarcie, obowiązująca dalej.", "admin")
    prompt_registry.clear_override("writer")
    assert admin.load_prompt_overrides() == 1 and prompt_registry.get("writer").startswith("Wersja po restarcie")
    listed = {p["role"]: p for p in admin.list_prompts()}
    assert listed["writer"]["overridden"] and listed["writer"]["versions"] == 4 and "task_planner" in listed


@pytest.mark.parametrize("role,text,user,message", [
    ("nie_ma_takiej", "Dowolny dłuższy tekst promptu.", "admin", "Nie ma wbudowanego"),
    ("writer", "krótki", "admin", "za krótki"),
    ("../etc", "Dowolny dłuższy tekst.", "admin", "Niepoprawna"),
    ("writer", "Dowolny dłuższy tekst promptu.", "uzytkownik", "Tylko administrator")])
def test_prompt_save_refusals(panel, role, text, user, message):
    with pytest.raises(admin.AdminError, match=message):
        admin.save_prompt(role, text, user)


# --------------------------------------------------------------------------- custom nodes

def test_user_node_is_private_and_runs_in_their_chats(panel, llm, invoke):
    admin.create_custom_node(NODE, "uzytkownik")
    folder = panel / "uzytkownik" / "streszczacz"
    cfg = json.loads((folder / "hapilot_extension.json").read_text())["config"]
    assert cfg["owner"] == "uzytkownik" and cfg["global"] is False and cfg["custom"] is True
    mine = ids("uzytkownik")["streszczacz"]
    assert mine["kind"] == "custom" and mine["mine"] and mine["editable"] and not mine["global"] and not mine["can_share"]
    assert "streszczacz" not in ids("admin") and "streszczacz" not in ids("anna")  # private: not even admins see it

    manager, app = admin.agent_for("uzytkownik")
    assert manager.tool_node_map()["streszczacz"] == "streszczacz"
    assert "streszczacz" not in admin.agent_for("anna")[0].tool_node_map()

    from src import run_context

    llm.on("task_planner", '{"steps": [{"id": 1, "tool": "streszczacz", "action": "streść umowę", "input": "umowa serwisowa"}]}')
    llm.on("streszczacz", "1. Umowa na serwis. 2. Rok. 3. Płatność miesięczna. 4. SLA 8h. 5. Wypowiedzenie 1 mies.")
    from langchain_core.messages import HumanMessage

    state = app.invoke({"messages": [HumanMessage(content="Streść umowę serwisową w punktach")], "task_plan": {},
                        "current_step": None, "retry_count": 0, "hitl_request": None, "hitl_response": None,
                        "last_generated_file": "", "halt": False},
                       config=run_context.make_config("s-private", profile="reference", extension_manager=manager))
    assert state["messages"][-1].content.startswith("1. Umowa na serwis")
    assert "Krótkie streszczenie umowy" in llm.prompts_for("task_planner")[0]
    prompt = llm.prompts_for("streszczacz")[0]
    assert "streszcza umowy w pięciu punktach" in prompt and "Streść umowę serwisową" in prompt
    role, spec = next((r, s) for r, s in llm.specs if r == "streszczacz")
    assert "coder" in spec.model and spec.temperature == 0.1
    # the shared graph (evaluation, no login) never has it
    from src.core.extension_manager import extension_manager

    assert "streszczacz" not in extension_manager.tool_node_map()


def test_only_admins_create_global_nodes_and_global_nodes_reach_everyone(panel):
    with pytest.raises(admin.AdminError, match="Tylko administrator"):
        admin.create_custom_node({**NODE, "global": True}, "uzytkownik")
    admin.create_custom_node({**NODE, "global": True}, "admin")
    for user in ("admin", "uzytkownik", "anna"):
        node = ids(user)["streszczacz"]
        assert node["global"] and node["editable"] == (user == "admin")
    with pytest.raises(admin.AdminError, match="administrator"):
        admin.update_custom_node("streszczacz", {**NODE, "label": "Moje"}, "uzytkownik")
    with pytest.raises(admin.AdminError, match="administrator"):
        admin.delete_custom_node("streszczacz", "uzytkownik")


def test_admin_switches_own_node_global_on_and_off(panel):
    admin.create_custom_node(NODE, "admin")
    assert "streszczacz" not in ids("uzytkownik")
    admin.set_global("streszczacz", True, "admin")
    assert ids("uzytkownik")["streszczacz"]["global"]
    admin.set_global("streszczacz", False, "admin")
    assert "streszczacz" not in ids("uzytkownik")
    admin.create_custom_node({**NODE, "name": "notatnik"}, "uzytkownik")
    with pytest.raises(admin.AdminError, match="Tylko administrator"):
        admin.set_global("notatnik", True, "uzytkownik")
    with pytest.raises(admin.AdminError, match="Nie ma takiego"):  # admins cannot reach other users' private nodes
        admin.set_global("notatnik", True, "admin")
    assert [e["action"] for e in admin.audit_log("admin")][:3] == ["node_created", "node_unshared", "node_shared"]
    assert [e["user"] for e in admin.audit_log("uzytkownik")] == ["uzytkownik"]  # users see only their own entries


def test_owner_edits_node_and_the_agent_follows(panel, llm):
    admin.create_custom_node(NODE, "uzytkownik")
    first = admin.agent_for("uzytkownik")[0]
    admin.update_custom_node("streszczacz", {**NODE, "label": "🧾 Streszczacz 2", "prompt": "Nowy prompt: streszczaj w trzech punktach.",
                                             "model_role": "default", "temperature": 0.5}, "uzytkownik")
    manager = admin.agent_for("uzytkownik")[0]
    assert manager is not first  # rebuilt because the files changed
    node = ids("uzytkownik")["streszczacz"]
    assert node["label"] == "🧾 Streszczacz 2" and node["prompt"].startswith("Nowy prompt") and node["config"]["temperature"] == 0.5
    assert prompt_registry.get("streszczacz").startswith("Nowy prompt")
    with pytest.raises(admin.AdminError, match="Prompt systemowy"):
        admin.update_custom_node("streszczacz", {**NODE, "prompt": "krótko"}, "uzytkownik")
    assert ids("uzytkownik")["streszczacz"]["prompt"].startswith("Nowy prompt")  # a refused edit changes nothing
    with pytest.raises(admin.AdminError, match="Nie ma takiego"):
        admin.update_custom_node("streszczacz", NODE, "anna")
    assert admin.audit_log("uzytkownik")[0]["action"] == "node_updated"


@pytest.mark.parametrize("change,message", [({"name": "writer"}, "zajęta"), ({"name": "Zła Nazwa"}, "Nazwa techniczna"),
                                            ({"prompt": "za krótko"}, "Prompt systemowy"), ({"model_role": "gpt"}, "Model"),
                                            ({"temperature": 3}, "Temperatura"), ({"label": ""}, "Etykieta")])
def test_invalid_custom_nodes_are_refused_and_leave_nothing(panel, change, message):
    with pytest.raises(admin.AdminError, match=message):
        admin.create_custom_node({**NODE, **change}, "uzytkownik")
    assert not list(panel.glob("*/*/hapilot_extension.json"))


def test_node_names_are_unique_across_users(panel):
    admin.create_custom_node(NODE, "uzytkownik")
    with pytest.raises(admin.AdminError, match="zajęta"):
        admin.create_custom_node(NODE, "anna")


def test_owner_deletes_node_others_cannot(panel):
    admin.create_custom_node(NODE, "uzytkownik")
    with pytest.raises(admin.AdminError):
        admin.delete_custom_node("streszczacz", "anna")
    with pytest.raises(admin.AdminError):
        admin.delete_custom_node("streszczacz", "admin")
    admin.delete_custom_node("streszczacz", "uzytkownik")
    assert not (panel / "uzytkownik" / "streszczacz").exists() and "streszczacz" not in ids("uzytkownik")
    assert not prompt_registry.has("streszczacz")
    for name in ("writer", "task_planner", "../writer", "streszczacz"):
        with pytest.raises(admin.AdminError):
            admin.delete_custom_node(name, "uzytkownik")


def test_graph_edges_match_the_compiled_graph(panel):
    admin.create_custom_node(NODE, "uzytkownik")
    data = admin.graph_data("uzytkownik")
    compiled = {tuple(e) for e in data["compiled_edges"]}
    concrete = [(e["source"], e["target"]) for e in data["edges"] if "__next__" not in (e["source"], e["target"])]
    assert concrete and set(concrete) <= compiled  # every drawn edge exists in LangGraph
    kinds = {(e["source"], e["target"]): e["kind"] for e in data["edges"]}
    assert kinds[("coder", "execute")] == "then" and kinds[("execute", "coder")] == "retry"
    assert kinds[("router", "streszczacz")] == "route" and kinds[("streszczacz", "__next__")] == "next"
    assert {("writer", "__end__"), ("writer", "streszczacz"), ("streszczacz", "writer")} <= compiled
    assert data["user"] == "uzytkownik" and data["admin"] is False


def test_evaluation_runs_never_see_custom_nodes():
    from src.config import PROJECT_ROOT

    eval_code = "".join(p.read_text(encoding="utf-8") for p in (PROJECT_ROOT / "src" / "eval").glob("*.py"))
    assert "enable_for_ui" not in eval_code and "src.admin" not in eval_code and "agent_for" not in eval_code
