"""Extension SDK (manifest v1): validation, catalog, edges, aliases, hooks, the example extension."""

import json
import shutil
from pathlib import Path

import pytest

from src.core.extension_manager import ExtensionManager, extension_manager, main, set_active, validate_manifest

ROOT = Path(__file__).resolve().parents[2]
BUILTIN = ROOT / "extensions"
EXAMPLES = ROOT / "examples" / "extensions"


@pytest.fixture
def restore_active():
    yield
    from src.agent_graph import reset_app

    set_active(extension_manager)
    reset_app()


def _ext(base: Path, ext_id: str, manifest: dict, files: dict | None = None) -> Path:
    d = base / ext_id
    d.mkdir(parents=True)
    (d / "hapilot_extension.json").write_text(json.dumps({"manifest_version": 1, "id": ext_id, "name": ext_id, **manifest}))
    for name, text in (files or {}).items():
        (d / name).write_text(text)
    return d


NODE_PY = "from langchain_core.messages import AIMessage\n\ndef run(state):\n    return {'messages': [AIMessage(content='x')]}\n"


def test_builtin_extensions_are_valid_and_loaded():
    for manifest in BUILTIN.glob("*/hapilot_extension.json"):
        assert validate_manifest(json.loads(manifest.read_text())) == [], manifest
    m = ExtensionManager([BUILTIN])
    m.discover_and_load()
    assert m.errors == []
    assert {"chat", "writer", "rag_knowledge", "document_generator", "document_modifier", "python_coder"} <= {x.id for x in m.get_all_manifests()}
    assert "n8n_operations" not in {x.id for x in m.get_all_manifests()}  # disabled
    tools = {t.name: t for t in m.get_tools()}
    assert tools["rag_search"].when_to_use and tools["template_pdf"].when_not_to_use and tools["hitl_confirm"].extension_id == "core"
    mapping = m.tool_node_map()
    assert mapping["python"] == "coder" and mapping["coder"] == "coder" and mapping["email"] == "writer"


def test_planner_prompt_lists_the_manifest_catalog(graph, invoke, llm):
    llm.on("task_planner", '{"steps": [{"id": 1, "tool": "chat", "action": "x", "input": ""}]}')
    llm.on("chat", "hej")
    invoke(graph, "Cześć", "s-catalog")
    prompt = llm.prompts_for("task_planner")[0]
    assert "- rag_search: Wyszukiwanie w dokumentach firmy" in prompt and "KIEDY NIE:" in prompt
    assert "- email:" not in prompt  # n8n is disabled
    assert "Jesteś Hapilot — planer zadań" in prompt  # the core planner prompt is registered


@pytest.mark.parametrize("manifest,files,error", [
    ({"nodes": [{"name": "a_node", "entry_point": "nodes"}]}, {}, "invalid manifest"),
    ({"nodes": [{"name": "a_node", "entry_point": "nodes:missing"}]}, {"nodes.py": NODE_PY}, "has no 'missing'"),
    ({"nodes": [{"name": "a_node", "entry_point": "nodes:run"}], "tools": [{"name": "t_x", "node": "other", "description": "d"}]},
     {"nodes.py": NODE_PY}, "needs a node"),
    ({"nodes": [{"name": "a_node", "entry_point": "nodes:run", "then": "nowhere"}]}, {"nodes.py": NODE_PY}, "unknown nodes"),
    ({"nodes": [{"name": "chat", "entry_point": "nodes:run"}]}, {"nodes.py": NODE_PY}, "names already used"),
    ({"nodes": [{"name": "a_node", "entry_point": "nodes:run"}], "prompts": {"x": "prompts/x.txt"}}, {"nodes.py": NODE_PY}, "prompt file"),
    ({"nodes": [{"name": "a_node", "entry_point": "nodes:run"}], "hooks": "nodes:run"}, {"nodes.py": NODE_PY}, "BaseExtension"),
])
def test_broken_extensions_are_skipped_with_a_reason(tmp_path, manifest, files, error):
    _ext(tmp_path, "broken_ext", manifest, files)
    m = ExtensionManager([BUILTIN, tmp_path])
    m.discover_and_load()
    assert "broken_ext" not in {x.id for x in m.get_all_manifests()}
    assert any(error in e for e in m.errors), m.errors
    assert "rag_knowledge" in {x.id for x in m.get_all_manifests()}  # the others still load


def test_example_extension_end_to_end(tmp_path, llm, restore_active):
    from langchain_core.messages import HumanMessage

    from src import run_context
    from src.agent_graph import build_dynamic_graph

    m = ExtensionManager([BUILTIN, EXAMPLES])
    m.discover_and_load()
    assert m.errors == [] and "vat" in m.tool_node_map()
    app = build_dynamic_graph(m)
    llm.on("task_planner", '{"steps": [{"id": 1, "tool": "vat", "action": "przelicz", "input": "1000 zł netto, VAT 23%"}]}')
    state = app.invoke({"messages": [HumanMessage(content="Ile to brutto: 1000 zł netto?")], "task_plan": {}, "current_step": None,
                        "retry_count": 0, "hitl_request": None, "hitl_response": None, "last_generated_file": "", "halt": False},
                       config=run_context.make_config("s-vat"))
    assert "brutto: 1 230,00 zł" in state["messages"][-1].content
    assert "- vat: Przeliczenie VAT bez modelu językowego" in llm.prompts_for("task_planner")[0]
    hooks = m._hooks["vat_calculator"]
    assert hooks.loaded and hooks.runs == 1


def test_vat_inverse_and_rate():
    import importlib.util

    spec = importlib.util.spec_from_file_location("vat_nodes", EXAMPLES / "vat_calculator" / "nodes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "netto: 1 000,00 zł" in mod.calculate("Ile wynosi netto z brutto 1230 zł?")
    assert "VAT: 80,00 zł" in mod.calculate("1000 zł, stawka 8%")
    # found on the GPU host: the label next to the amount decides the direction
    assert "netto: 1 500,00 zł" in mod.calculate("Faktura opiewa na 1845 zł brutto (VAT 23%). Ile wynosi kwota netto?")
    assert "brutto: 3 013,50 zł" in mod.calculate("Ile to brutto: 2450 zł netto przy stawce VAT 23%?")
    # the planner's wording of the same requests (label before the amount)
    assert "netto: 1 500,00 zł" in mod.calculate("Oblicz kwotę netto dla kwoty brutto 1845 zł przy stawce VAT 23%")
    assert "brutto: 3 013,50 zł" in mod.calculate("Oblicz kwotę brutto dla kwoty netto 2450 zł przy stawce VAT 23%")


def test_before_route_hook_can_redirect(tmp_path, llm, restore_active):
    from langchain_core.messages import HumanMessage

    from src import run_context
    from src.agent_graph import build_dynamic_graph

    hooks_py = ("from src.core.base_extension import BaseExtension\n\nclass H(BaseExtension):\n"
                "    def on_before_route(self, state):\n        return {'next_step': 'chat'}\n")
    _ext(tmp_path, "redirect_ext", {"hooks": "hooks:H"}, {"hooks.py": hooks_py})
    m = ExtensionManager([BUILTIN, tmp_path])
    m.discover_and_load()
    assert m.errors == []
    app = build_dynamic_graph(m)
    llm.on("task_planner", '{"steps": [{"id": 1, "tool": "rag_search", "action": "x", "input": "x"}]}')
    llm.on("chat", "przekierowano")
    state = app.invoke({"messages": [HumanMessage(content="Pytanie")], "task_plan": {}, "current_step": None, "retry_count": 0,
                        "hitl_request": None, "hitl_response": None, "last_generated_file": "", "halt": False},
                       config=run_context.make_config("s-hook"))
    assert state["messages"][-1].content == "przekierowano"


def test_alias_routes_to_the_tool_node(graph, invoke, llm, docker):
    llm.on("task_planner", '{"steps": [{"id": 1, "tool": "coder", "action": "policz", "input": "policz 2+2"}]}')
    llm.on("coder", "```python\nprint(2 + 2)\n```")
    state = invoke(graph, "Policz 2+2", "s-alias")
    assert "SUKCES" in state["messages"][-1].content and "4" in state["messages"][-1].content


def test_extensions_dirs_from_environment(monkeypatch):
    from src.core.extension_manager import default_dirs

    monkeypatch.setenv("HAPILOT_EXTENSIONS_DIRS", f"{EXAMPLES}")
    assert default_dirs()[-1] == EXAMPLES


def test_check_command(tmp_path, capsys):
    assert main([str(BUILTIN)]) == 0
    _ext(tmp_path, "bad_one", {"nodes": [{"name": "x_node", "entry_point": "nope"}]})
    assert main([str(tmp_path)]) == 1
    assert "ERROR: bad_one" in capsys.readouterr().out


def test_copying_the_example_is_enough(tmp_path):
    shutil.copytree(EXAMPLES / "vat_calculator", tmp_path / "vat_calculator")
    m = ExtensionManager([tmp_path])
    m.discover_and_load()
    assert m.errors == [] and m.tool_node_map() == {"vat": "vat", "hitl_confirm": "hitl_confirm"}
