"""Administration of the agent from the UI: custom nodes, prompt overrides, the agent graph, an audit log.

Used by the admin panel (src/ui_admin.py); no Chainlit here. Everything an administrator changes lives
under the data directory and applies to the UI only:

- custom nodes: ordinary extensions in ``custom_extensions/<node>/`` (manifest, prompt, a one-line
  ``nodes.py`` calling src/core/custom_node.py). They are prompt-only: one model call, no code, no tools;
- prompt overrides: ``prompt_overrides/<role>.json`` with the full version history (the last entry is
  in force; an entry with ``"text": null`` restores the default);
- audit log: ``admin_audit.jsonl`` (who, when, what).

The UI calls ``enable_for_ui()`` at start-up, which adds the custom extensions folder to the extension
search path and applies the prompt overrides. Evaluation runs (src/eval) never call it, so benchmark
results depend only on the repository.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.config import get_settings
from src.core.prompt_registry import prompt_registry

NAME = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
ROLE = re.compile(r"^[a-z][a-z0-9_]{1,60}$")
MODEL_ROLES = {"default": "Model ogólny profilu", "coder": "Model do kodu (profil: rola coder)"}
CORE_NODES = {
    "task_planner": ("📋 Planer zadań", "Rozkłada prośbę użytkownika na kroki i dobiera narzędzia", ["task_planner"]),
    "router": ("🔀 Router", "Kieruje bieżący krok planu do węzła jego narzędzia", []),
    "hitl_confirm": ("🤝 Potwierdzenie", "Zatrzymuje plan i pyta użytkownika o zgodę", []),
}
_lock = threading.RLock()


class AdminError(ValueError):
    """A request the panel must refuse; the message is shown to the administrator (Polish)."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- places and people

def custom_dir() -> Path:
    value = os.environ.get("HAPILOT_CUSTOM_EXTENSIONS_DIR", "").strip()
    return Path(value).expanduser() if value else get_settings().data_dir / "custom_extensions"


def overrides_dir() -> Path:
    return get_settings().data_dir / "prompt_overrides"


def audit_path() -> Path:
    return get_settings().data_dir / "admin_audit.jsonl"


def admins() -> set[str]:
    """Logins with admin rights: ``HAPILOT_UI_ADMINS`` (comma-separated), default ``admin``."""
    value = os.environ.get("HAPILOT_UI_ADMINS")
    names = value if value is not None else "admin"
    return {n.strip() for n in names.split(",") if n.strip()}


def is_admin(identifier: Optional[str]) -> bool:
    return bool(identifier) and identifier in admins()


def enable_for_ui() -> None:
    """Custom nodes and prompt overrides for the UI process (call before the graph is built)."""
    folder = str(custom_dir())
    parts = [p for p in os.environ.get("HAPILOT_EXTENSIONS_DIRS", "").split(os.pathsep) if p.strip()]
    if folder not in parts:
        os.environ["HAPILOT_EXTENSIONS_DIRS"] = os.pathsep.join(parts + [folder])
    custom_dir().mkdir(parents=True, exist_ok=True)
    load_prompt_overrides()


# --------------------------------------------------------------------------- audit

def audit(user: str, action: str, target: str, detail: str = "") -> None:
    path = audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": now(), "user": user, "action": action, "target": target, "detail": detail},
                            ensure_ascii=False) + "\n")


def audit_log(limit: int = 200) -> list[dict]:
    path = audit_path()
    if not path.is_file():
        return []
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in reversed(lines[-limit:])]


# --------------------------------------------------------------------------- prompts

def _override_file(role: str) -> Path:
    if not ROLE.match(role or ""):
        raise AdminError(f"Niepoprawna nazwa roli: {role!r}")
    return overrides_dir() / f"{role}.json"


def prompt_history(role: str) -> list[dict]:
    path = _override_file(role)
    return json.loads(path.read_text(encoding="utf-8")).get("history", []) if path.is_file() else []


def load_prompt_overrides() -> int:
    """Apply the latest override of every role; returns how many are in force."""
    count = 0
    folder = overrides_dir()
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        history = json.loads(path.read_text(encoding="utf-8")).get("history", [])
        if history and history[-1].get("text") is not None:
            prompt_registry.set_override(path.stem, history[-1]["text"])
            count += 1
        else:
            prompt_registry.clear_override(path.stem)
    return count


def _append_version(role: str, user: str, text: Optional[str]) -> None:
    path = _override_file(role)
    path.parent.mkdir(parents=True, exist_ok=True)
    history = prompt_history(role) + [{"ts": now(), "user": user, "text": text}]
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"role": role, "history": history}, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _known_role(role: str) -> None:
    _override_file(role)
    if prompt_registry.default(role) is None:
        raise AdminError(f"Nie ma promptu o nazwie {role!r}.")


def save_prompt(role: str, text: str, user: str) -> dict:
    with _lock:
        _known_role(role)
        text = (text or "").replace("\r\n", "\n")
        if len(text.strip()) < 10:
            raise AdminError("Prompt jest za krótki (minimum 10 znaków).")
        if len(text) > 50_000:
            raise AdminError("Prompt jest za długi (maksimum 50 000 znaków).")
        if text == prompt_registry.get(role):
            return prompt_info(role)
        _append_version(role, user, text)
        prompt_registry.set_override(role, text)
        audit(user, "prompt_saved", role, f"{len(text)} znaków")
        return prompt_info(role)


def restore_prompt(role: str, user: str) -> dict:
    with _lock:
        _known_role(role)
        if prompt_registry.overridden(role):
            _append_version(role, user, None)
            prompt_registry.clear_override(role)
            audit(user, "prompt_restored", role, "przywrócono domyślny")
        return prompt_info(role)


def _prompt_users() -> dict[str, list[str]]:
    users: dict[str, list[str]] = {}
    for node in graph_nodes():
        for role in node["prompts"]:
            users.setdefault(role, []).append(node["id"])
    return users


def prompt_info(role: str) -> dict:
    _known_role(role)
    history = prompt_history(role)
    return {"role": role, "source": prompt_registry.get_source(role), "default": prompt_registry.default(role),
            "current": prompt_registry.get(role), "overridden": prompt_registry.overridden(role),
            "nodes": _prompt_users().get(role, []),
            "history": [{"index": i, "ts": h["ts"], "user": h["user"], "restored": h.get("text") is None,
                         "chars": len(h["text"]) if h.get("text") else 0, "text": h.get("text")}
                        for i, h in enumerate(history)][::-1]}


def list_prompts() -> list[dict]:
    users = _prompt_users()
    result = []
    for role in prompt_registry.list_roles():
        default = prompt_registry.default(role)
        if default is None:
            continue
        current = prompt_registry.get(role) or ""
        result.append({"role": role, "source": prompt_registry.get_source(role), "overridden": prompt_registry.overridden(role),
                       "chars": len(current), "nodes": users.get(role, []), "versions": len(prompt_history(role))})
    return result


# --------------------------------------------------------------------------- custom nodes

@dataclass
class NodeDraft:
    name: str
    label: str
    description: str
    prompt: str
    when_to_use: str = ""
    when_not_to_use: str = ""
    example: str = ""
    model_role: str = "default"
    temperature: float = 0.3

    @classmethod
    def from_dict(cls, data: dict) -> "NodeDraft":
        try:
            temperature = float(data.get("temperature", 0.3))
        except (TypeError, ValueError):
            raise AdminError("Temperatura musi być liczbą.") from None
        return cls(name=str(data.get("name", "")).strip(), label=str(data.get("label", "")).strip(),
                   description=str(data.get("description", "")).strip(), prompt=str(data.get("prompt", "")).replace("\r\n", "\n"),
                   when_to_use=str(data.get("when_to_use", "")).strip(), when_not_to_use=str(data.get("when_not_to_use", "")).strip(),
                   example=str(data.get("example", "")).strip(), model_role=str(data.get("model_role", "default")),
                   temperature=temperature)


def _taken_names() -> set[str]:
    from src.core.extension_manager import get_active

    manager = get_active()
    names = set(CORE_NODES) | set(manager.get_node_specs()) | {t.name for t in manager.get_tools()}
    names |= {m.id for m in manager.get_all_manifests()} | set(prompt_registry.list_roles())
    names |= {a for t in manager.get_tools() for a in t.aliases}
    if custom_dir().is_dir():
        names |= {p.name for p in custom_dir().iterdir()}
    return names | {"start", "end", "default", "coder"}


def validate_draft(draft: NodeDraft) -> list[str]:
    problems = []
    if not NAME.match(draft.name):
        problems.append("Nazwa techniczna: 3–41 znaków, małe litery bez polskich znaków, cyfry i _, zaczyna się literą.")
    elif draft.name in _taken_names():
        problems.append(f"Nazwa {draft.name!r} jest już zajęta przez inny węzeł, narzędzie lub prompt.")
    if not draft.label or len(draft.label) > 60:
        problems.append("Etykieta: od 1 do 60 znaków.")
    if not draft.description or len(draft.description) > 600:
        problems.append("Opis dla planera: od 1 do 600 znaków.")
    for value, what in ((draft.when_to_use, "Kiedy używać"), (draft.when_not_to_use, "Kiedy nie używać"), (draft.example, "Przykład")):
        if len(value) > 600:
            problems.append(f"{what}: najwyżej 600 znaków.")
    if len(draft.prompt.strip()) < 20 or len(draft.prompt) > 50_000:
        problems.append("Prompt systemowy: od 20 do 50 000 znaków.")
    if draft.model_role not in MODEL_ROLES:
        problems.append("Model: wybierz model ogólny albo model do kodu.")
    if not 0.0 <= draft.temperature <= 1.5:
        problems.append("Temperatura: od 0 do 1,5.")
    return problems


def _manifest(draft: NodeDraft, user: str) -> dict:
    tool = {"name": draft.name, "description": draft.description}
    for key in ("when_to_use", "when_not_to_use", "example"):
        if getattr(draft, key):
            tool[key] = getattr(draft, key)
    return {"manifest_version": 1, "id": draft.name, "name": draft.label, "version": "1.0.0",
            "description": draft.description, "author": user, "license": "MIT", "enabled": True,
            "nodes": [{"name": draft.name, "entry_point": "nodes:run", "label": draft.label, "description": draft.description}],
            "tools": [tool], "prompts": {draft.name: f"prompts/{draft.name}.txt"},
            "config": {"custom": True, "model_role": draft.model_role, "temperature": draft.temperature,
                       "created_by": user, "created_at": now()}}


NODE_MODULE = '''"""Custom node created in the admin panel; behaviour: src/core/custom_node.py."""

from src.core.custom_node import run_custom_node


def run(state):
    return run_custom_node(state, "{name}")
'''


def create_custom_node(data: dict, user: str) -> dict:
    """Write the extension, rebuild the graph and check the node is in it; nothing is left behind on failure."""
    from src.core.extension_manager import validate_manifest

    with _lock:
        draft = NodeDraft.from_dict(data)
        problems = validate_draft(draft)
        manifest = _manifest(draft, user)
        problems += [f"Manifest: {p}" for p in validate_manifest(manifest)]
        if problems:
            raise AdminError("\n".join(problems))
        folder = custom_dir() / draft.name
        (folder / "prompts").mkdir(parents=True)
        try:
            (folder / "prompts" / f"{draft.name}.txt").write_text(draft.prompt, encoding="utf-8")
            (folder / "nodes.py").write_text(NODE_MODULE.format(name=draft.name), encoding="utf-8")
            (folder / "hapilot_extension.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            manager = rebuild()
            if draft.name not in manager.get_node_specs():
                raise AdminError("Hapilot nie przyjął węzła: " + "; ".join(e for e in manager.errors if draft.name in e))
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            prompt_registry.unregister(draft.name)
            rebuild()
            raise
        audit(user, "node_created", draft.name, f"{draft.label}; model {draft.model_role}, temp. {draft.temperature}")
        return asdict(draft)


def is_custom(name: str) -> bool:
    manifest = custom_dir() / name / "hapilot_extension.json"
    if not NAME.match(name or "") or not manifest.is_file():
        return False
    try:
        return bool(json.loads(manifest.read_text(encoding="utf-8")).get("config", {}).get("custom"))
    except (OSError, ValueError):
        return False


def delete_custom_node(name: str, user: str) -> None:
    with _lock:
        if not is_custom(name):
            raise AdminError("Można usuwać tylko węzły własne utworzone w panelu.")
        folder = (custom_dir() / name).resolve()
        if folder.parent != custom_dir().resolve():
            raise AdminError("Niepoprawna ścieżka węzła.")
        shutil.rmtree(folder)
        prompt_registry.unregister(name)
        override = overrides_dir() / f"{name}.json"
        if override.is_file():
            override.unlink()
        rebuild()
        audit(user, "node_deleted", name)


def rebuild():
    from src.agent_graph import rebuild_app

    return rebuild_app()


# --------------------------------------------------------------------------- graph

def graph_nodes() -> list[dict]:
    from src.agent_graph import get_app
    from src.core.extension_manager import get_active

    get_app()
    manager = get_active()
    manifests = {m.id: m for m in manager.get_all_manifests()}
    tools_by_node: dict[str, list[dict]] = {}
    for t in manager.get_tools():
        tools_by_node.setdefault(t.node, []).append({"name": t.name, "description": t.description,
                                                     "when_to_use": t.when_to_use, "when_not_to_use": t.when_not_to_use})
    nodes = [{"id": nid, "label": label, "description": desc, "kind": "core", "extension": "core",
              "extension_name": "Rdzeń Hapilota", "prompts": prompts, "tools": tools_by_node.get(nid, []),
              "policy": None, "then": None, "custom": False, "config": {}}
             for nid, (label, desc, prompts) in CORE_NODES.items()]
    for name, spec in manager.get_node_specs().items():
        m = manifests.get(spec.extension_id)
        roles = list((m.raw.get("prompts") or {}) if m else {})
        ext_nodes = [n["name"] for n in (m.raw.get("nodes") or [])] if m else [name]
        if len(ext_nodes) > 1:  # several nodes share an extension: give each the prompts named after it
            mine = [r for r in roles if r == name or r.startswith(name + "_")]
            roles = mine or (roles if name == ext_nodes[0] else [])
        config = manager.get_config(spec.extension_id)
        custom = bool(config.get("custom"))
        nodes.append({"id": name, "label": spec.label or name, "description": spec.description, "kind": "custom" if custom else "extension",
                      "extension": spec.extension_id, "extension_name": m.name if m else spec.extension_id,
                      "prompts": roles, "tools": tools_by_node.get(name, []), "then": spec.then,
                      "policy": None if spec.then else dict(spec.after), "custom": custom,
                      "config": {k: config[k] for k in ("model_role", "temperature", "created_by", "created_at") if k in config}})
    return nodes


def graph_data() -> dict:
    """Nodes and edges for the admin panel, plus the edges of the compiled LangGraph graph (to check against)."""
    from src.agent_graph import get_app

    nodes = graph_nodes()
    workers = [n for n in nodes if n["id"] not in ("task_planner", "router")]
    edges = [{"source": "__start__", "target": "task_planner", "kind": "always", "label": ""},
             {"source": "task_planner", "target": "router", "kind": "always", "label": "plan gotowy"},
             {"source": "task_planner", "target": "__end__", "kind": "stop", "label": "pytanie do użytkownika"}]
    for n in workers:
        tools = ", ".join(t["name"] for t in n["tools"])
        if n["id"] == "hitl_confirm" or n["tools"]:
            edges.append({"source": "router", "target": n["id"], "kind": "route", "label": tools})
        if n["then"]:
            edges.append({"source": n["id"], "target": n["then"], "kind": "then", "label": "zawsze"})
            continue
        policy = (n["policy"] or {}).get("policy", "plan")
        if policy == "retry":
            edges.append({"source": n["id"], "target": n["policy"]["retry_to"], "kind": "retry",
                          "label": f"błąd → ponów (do {n['policy'].get('max_attempts', 3)}×)"})
        edges.append({"source": n["id"], "target": "__next__", "kind": "next",
                      "label": "po sukcesie" if policy in ("on_success", "retry") else ""})
    edges += [{"source": "__next__", "target": "router", "kind": "loop", "label": "kolejny krok planu"},
              {"source": "__next__", "target": "__end__", "kind": "always", "label": "koniec planu"}]
    graph = get_app().get_graph()
    compiled = sorted({(e.source, e.target) for e in graph.edges})
    return {"nodes": nodes, "edges": edges, "compiled_edges": [list(e) for e in compiled],
            "custom_dir": str(custom_dir()), "model_roles": MODEL_ROLES}
