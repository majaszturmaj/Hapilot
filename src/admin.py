"""The agent panel behind /hapilot/admin (src/ui_admin.py): the agent graph, custom nodes, prompts, an audit log.

No Chainlit here. Who may do what:

- every logged-in user sees the agent graph: the built-in nodes, all **global** custom nodes and their own
  **private** ones (never other users' private nodes), and can add, edit and delete their own custom nodes.
  A user's nodes are private: they take part only in that user's chats;
- administrators (``HAPILOT_UI_ADMINS``, default ``admin``) can also switch their nodes to global (on/off):
  a global node takes part in every user's chats, and only an administrator can edit or delete it.
  Administrators also edit the prompts of the built-in nodes (with version history and restore);
  everyone else can read them.

Storage (under the data directory; the UI only — evaluation runs never load any of it):

- custom nodes: ``custom_extensions/<owner>/<node>/`` — an ordinary extension (manifest, prompt, a one-line
  ``nodes.py`` calling src/core/custom_node.py). Prompt-only: one model call, no code, no tools. Node names are
  unique across all users (a node may become global, and names are also planner tool names);
- prompt overrides of built-in nodes: ``prompt_overrides/<role>.json`` with the full history;
- audit log: ``admin_audit.jsonl``.

Each user's chats run on their own compiled graph (``agent_for``): the built-in extensions plus the custom nodes
that user may use. It is cached and rebuilt when one of those nodes changes on disk.
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
MANIFEST = "hapilot_extension.json"
MODEL_ROLES = {"default": "Model ogólny profilu", "coder": "Model do kodu (profil: rola coder)"}
CORE_NODES = {
    "task_planner": ("📋 Planer zadań", "Rozkłada prośbę użytkownika na kroki i dobiera narzędzia", ["task_planner"]),
    "router": ("🔀 Router", "Kieruje bieżący krok planu do węzła jego narzędzia", []),
    "hitl_confirm": ("🤝 Potwierdzenie", "Zatrzymuje plan i pyta użytkownika o zgodę", []),
}
BUILTIN_EXTENSIONS = (Path(__file__).resolve().parents[1] / "extensions").resolve()  # shipped with Hapilot
_lock = threading.RLock()
_agents: dict[str, tuple] = {}   # user -> (signature, manager, compiled graph)


class AdminError(ValueError):
    """A request the panel must refuse; the message is shown to the user (Polish)."""


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


def owner_folder(user: str) -> str:
    """A file-system-safe folder name for a login (the real login is kept in the manifest)."""
    return re.sub(r"[^a-z0-9_-]", "_", (user or "").lower())[:40] or "_"


def enable_for_ui() -> None:
    """Apply the prompt overrides of the built-in nodes (the UI process only)."""
    custom_dir().mkdir(parents=True, exist_ok=True)
    load_prompt_overrides()


# --------------------------------------------------------------------------- audit

def audit(user: str, action: str, target: str, detail: str = "") -> None:
    path = audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": now(), "user": user, "action": action, "target": target, "detail": detail},
                            ensure_ascii=False) + "\n")


def audit_log(user: Optional[str] = None, limit: int = 200) -> list[dict]:
    """Newest first; administrators (or ``user=None``) see everything, other users their own entries."""
    path = audit_path()
    if not path.is_file():
        return []
    entries = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if user is not None and not is_admin(user):
        entries = [e for e in entries if e.get("user") == user]
    return entries[-limit:][::-1]


# --------------------------------------------------------------------------- built-in prompts

def _builtin():
    """The shared extension manager (built-in extensions only), loaded."""
    from src.agent_graph import get_app
    from src.core.extension_manager import extension_manager

    get_app()
    return extension_manager


def _builtin_role(role: str) -> bool:
    """A prompt of a built-in node (custom nodes' prompts are edited with the node)."""
    return prompt_registry.default(role) is not None and not _custom_index().get(role)


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
    _builtin()
    if not _builtin_role(role):
        raise AdminError(f"Nie ma wbudowanego promptu o nazwie {role!r}.")


def _require_admin(user: str) -> None:
    if not is_admin(user):
        raise AdminError("Tylko administrator może to zrobić.")


def save_prompt(role: str, text: str, user: str) -> dict:
    with _lock:
        _require_admin(user)
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
        _require_admin(user)
        _known_role(role)
        if prompt_registry.overridden(role):
            _append_version(role, user, None)
            prompt_registry.clear_override(role)
            audit(user, "prompt_restored", role, "przywrócono domyślny")
        return prompt_info(role)


def _prompt_users() -> dict[str, list[str]]:
    users: dict[str, list[str]] = {}
    for node in _builtin_nodes(_builtin()):
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
    """Prompts of the built-in nodes."""
    _builtin()
    users = _prompt_users()
    result = []
    for role in prompt_registry.list_roles():
        if not _builtin_role(role):
            continue
        current = prompt_registry.get(role) or ""
        result.append({"role": role, "source": prompt_registry.get_source(role), "overridden": prompt_registry.overridden(role),
                       "chars": len(current), "nodes": users.get(role, []), "versions": len(prompt_history(role))})
    return result


# --------------------------------------------------------------------------- custom nodes: storage

def _custom_index() -> dict[str, tuple[Path, dict]]:
    """Every custom node on disk: name -> (folder, manifest)."""
    index = {}
    root = custom_dir()
    for manifest in sorted(root.glob(f"*/*/{MANIFEST}")) if root.is_dir() else []:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("config", {}).get("custom") and data.get("id") == manifest.parent.name:
            index[data["id"]] = (manifest.parent, data)
    return index


def _owner(data: dict) -> str:
    return data.get("config", {}).get("owner", "")


def _is_global(data: dict) -> bool:
    return bool(data.get("config", {}).get("global"))


def can_use(user: str, data: dict) -> bool:
    return _is_global(data) or _owner(data) == user


def can_edit(user: str, data: dict) -> bool:
    """Owners edit their private nodes; a global node only an administrator."""
    return is_admin(user) if _is_global(data) else _owner(data) == user


def _node(name: str, user: str) -> tuple[Path, dict]:
    found = _custom_index().get(name)
    if found is None or not can_use(user, found[1]):
        raise AdminError("Nie ma takiego węzła własnego.")
    return found


# --------------------------------------------------------------------------- custom nodes: per-user agents

def _signature(folders: list[Path]) -> tuple:
    sig = []
    for folder in folders:
        for path in sorted([folder / MANIFEST, *(folder / "prompts").glob("*.txt")]):
            try:
                sig.append((str(path), path.stat().st_mtime_ns))
            except OSError:
                pass
    return tuple(sig)


def agent_for(user: str):
    """(extension manager, compiled graph) for a user: built-in extensions + the custom nodes they may use."""
    from src.agent_graph import build_dynamic_graph
    from src.core.extension_manager import ExtensionManager

    folders = [folder for folder, data in _custom_index().values() if can_use(user, data)]
    signature = _signature(folders)
    with _lock:
        cached = _agents.get(user)
        if cached and cached[0] == signature:
            return cached[1], cached[2]
        manager = ExtensionManager(extra=folders)
        manager.discover_and_load()
        app = build_dynamic_graph(manager, activate=False)
        _agents[user] = (signature, manager, app)
        return manager, app


def forget_agents() -> None:
    with _lock:
        _agents.clear()


# --------------------------------------------------------------------------- custom nodes: create, edit, share, delete

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
    manager = _builtin()
    names = set(CORE_NODES) | set(manager.get_node_specs()) | {t.name for t in manager.get_tools()}
    names |= {m.id for m in manager.get_all_manifests()} | {a for t in manager.get_tools() for a in t.aliases}
    names |= {r for r in prompt_registry.list_roles() if _builtin_role(r)} | set(_custom_index())
    return names | {"start", "end", "default", "coder"}


def validate_draft(draft: NodeDraft, new: bool = True) -> list[str]:
    problems = []
    if not NAME.match(draft.name):
        problems.append("Nazwa techniczna: 3–41 znaków, małe litery bez polskich znaków, cyfry i _, zaczyna się literą.")
    elif new and draft.name in _taken_names():
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


def _manifest(draft: NodeDraft, config: dict) -> dict:
    tool = {"name": draft.name, "description": draft.description}
    for key in ("when_to_use", "when_not_to_use", "example"):
        if getattr(draft, key):
            tool[key] = getattr(draft, key)
    return {"manifest_version": 1, "id": draft.name, "name": draft.label, "version": "1.0.0",
            "description": draft.description, "author": config["owner"], "license": "MIT", "enabled": True,
            "nodes": [{"name": draft.name, "entry_point": "nodes:run", "label": draft.label, "description": draft.description}],
            "tools": [tool], "prompts": {draft.name: f"prompts/{draft.name}.txt"},
            "config": {**config, "custom": True, "model_role": draft.model_role, "temperature": draft.temperature}}


NODE_MODULE = '''"""Custom node created in the agent panel; behaviour: src/core/custom_node.py."""

from src.core.custom_node import run_custom_node


def run(state):
    return run_custom_node(state, "{name}")
'''


def _write(folder: Path, draft: NodeDraft, manifest: dict) -> None:
    (folder / "prompts").mkdir(parents=True, exist_ok=True)
    (folder / "prompts" / f"{draft.name}.txt").write_text(draft.prompt, encoding="utf-8")
    (folder / "nodes.py").write_text(NODE_MODULE.format(name=draft.name), encoding="utf-8")
    tmp = folder / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(folder / MANIFEST)


def _check_loads(name: str, user: str) -> None:
    manager, _app = agent_for(user)
    if name not in manager.get_node_specs():
        raise AdminError("Hapilot nie przyjął węzła: " + ("; ".join(e for e in manager.errors if name in e) or "nieznany błąd"))


def create_custom_node(data: dict, user: str) -> dict:
    """A new node of ``user`` (private; an administrator may create it global). Nothing is left behind on failure."""
    from src.core.extension_manager import validate_manifest

    with _lock:
        draft = NodeDraft.from_dict(data)
        make_global = bool(data.get("global"))
        if make_global and not is_admin(user):
            raise AdminError("Tylko administrator może udostępnić węzeł wszystkim użytkownikom.")
        problems = validate_draft(draft)
        manifest = _manifest(draft, {"owner": user, "global": make_global, "created_by": user, "created_at": now()})
        problems += [f"Manifest: {p}" for p in validate_manifest(manifest)]
        if problems:
            raise AdminError("\n".join(problems))
        folder = custom_dir() / owner_folder(user) / draft.name
        folder.mkdir(parents=True)
        try:
            _write(folder, draft, manifest)
            _check_loads(draft.name, user)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            prompt_registry.unregister(draft.name)
            raise
        audit(user, "node_created", draft.name, f"{draft.label}; {'globalny' if make_global else 'prywatny'}; "
                                               f"model {draft.model_role}, temp. {draft.temperature}")
        return {**asdict(draft), "global": make_global}


def update_custom_node(name: str, data: dict, user: str) -> dict:
    """Change a node's label, descriptions, prompt, model or temperature (the name stays)."""
    from src.core.extension_manager import validate_manifest

    with _lock:
        folder, old = _node(name, user)
        if not can_edit(user, old):
            raise AdminError("Ten węzeł może zmienić tylko jego autor (węzeł globalny — administrator).")
        draft = NodeDraft.from_dict({**data, "name": name})
        problems = validate_draft(draft, new=False)
        config = {k: v for k, v in old.get("config", {}).items() if k not in ("model_role", "temperature")}
        manifest = _manifest(draft, {**config, "updated_by": user, "updated_at": now()})
        problems += [f"Manifest: {p}" for p in validate_manifest(manifest)]
        if problems:
            raise AdminError("\n".join(problems))
        backup = {p.name: p.read_bytes() for p in (folder / MANIFEST, folder / "prompts" / f"{name}.txt") if p.is_file()}
        try:
            _write(folder, draft, manifest)
            _check_loads(name, user)
        except Exception:
            (folder / MANIFEST).write_bytes(backup[MANIFEST])
            (folder / "prompts" / f"{name}.txt").write_bytes(backup[f"{name}.txt"])
            raise
        prompt_registry.register(name, draft.prompt, source=name)
        audit(user, "node_updated", name, f"{draft.label}; model {draft.model_role}, temp. {draft.temperature}")
        return {**asdict(draft), "global": _is_global(manifest)}


def set_global(name: str, value: bool, user: str) -> dict:
    """An administrator shares their node with everyone (or takes it back)."""
    with _lock:
        _require_admin(user)
        folder, data = _node(name, user)
        if not (_owner(data) == user or _is_global(data)):
            raise AdminError("Udostępnić można tylko własny węzeł.")
        data["config"]["global"] = bool(value)
        tmp = folder / (MANIFEST + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(folder / MANIFEST)
        audit(user, "node_shared" if value else "node_unshared", name,
              "dostępny dla wszystkich" if value else "tylko dla autora")
        return {"name": name, "global": bool(value)}


def delete_custom_node(name: str, user: str) -> None:
    with _lock:
        if not NAME.match(name or ""):
            raise AdminError("Można usuwać tylko węzły własne utworzone w panelu.")
        folder, data = _node(name, user)
        if not can_edit(user, data):
            raise AdminError("Ten węzeł może usunąć tylko jego autor (węzeł globalny — administrator).")
        folder = folder.resolve()
        if folder.parent.parent != custom_dir().resolve():
            raise AdminError("Niepoprawna ścieżka węzła.")
        shutil.rmtree(folder)
        prompt_registry.unregister(name)
        audit(user, "node_deleted", name)


# --------------------------------------------------------------------------- graph

def _builtin_nodes(manager) -> list[dict]:
    """Core and extension nodes (no custom ones) of a manager, with their prompts and tools."""
    return [n for n in _nodes(manager, None) if n["kind"] != "custom"]


def _nodes(manager, user: Optional[str]) -> list[dict]:
    manifests = {m.id: m for m in manager.get_all_manifests()}
    custom = _custom_index()
    tools_by_node: dict[str, list[dict]] = {}
    for t in manager.get_tools():
        tools_by_node.setdefault(t.node, []).append({"name": t.name, "description": t.description, "example": t.example,
                                                     "when_to_use": t.when_to_use, "when_not_to_use": t.when_not_to_use})
    nodes = [{"id": nid, "label": label, "description": desc, "kind": "core", "extension": "core",
              "extension_name": "Rdzeń Hapilota", "prompts": prompts, "tools": tools_by_node.get(nid, []),
              "policy": None, "then": None, "config": {}}
             for nid, (label, desc, prompts) in CORE_NODES.items()]
    for name, spec in manager.get_node_specs().items():
        m = manifests.get(spec.extension_id)
        builtin = bool(m) and Path(m.directory).resolve().parent == BUILTIN_EXTENSIONS
        node = {"id": name, "label": spec.label or name, "description": spec.description, "kind": "extension",
                "builtin": builtin, "extension": spec.extension_id, "extension_name": m.name if m else spec.extension_id,
                "tools": tools_by_node.get(name, []), "then": spec.then,
                "policy": None if spec.then else dict(spec.after), "config": {}}
        if name in custom:
            folder, data = custom[name]
            cfg = data.get("config", {})
            node.update(kind="custom", prompts=[], prompt=(folder / "prompts" / f"{name}.txt").read_text(encoding="utf-8"),
                        owner=cfg.get("owner"), global_=bool(cfg.get("global")), mine=cfg.get("owner") == user,
                        editable=user is not None and can_edit(user, data), can_share=is_admin(user) and (cfg.get("owner") == user or cfg.get("global")),
                        config={k: cfg[k] for k in ("model_role", "temperature", "created_by", "created_at", "updated_by", "updated_at") if k in cfg})
            node["global"] = node.pop("global_")
        else:
            roles = list((m.raw.get("prompts") or {}) if m else {})
            ext_nodes = [n["name"] for n in (m.raw.get("nodes") or [])] if m else [name]
            if len(ext_nodes) > 1:  # several nodes share an extension: give each the prompts named after it
                mine = [r for r in roles if r == name or r.startswith(name + "_")]
                roles = mine or (roles if name == ext_nodes[0] else [])
            node["prompts"] = roles
        nodes.append(node)
    return nodes


def graph_nodes(user: str) -> list[dict]:
    manager, _app = agent_for(user)
    return _nodes(manager, user)


def graph_data(user: str) -> dict:
    """Nodes and edges of the user's agent, plus the edges of its compiled LangGraph graph (to check against)."""
    manager, app = agent_for(user)
    nodes = _nodes(manager, user)
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
    compiled = sorted({(e.source, e.target) for e in app.get_graph().edges})
    return {"nodes": nodes, "edges": edges, "compiled_edges": [list(e) for e in compiled],
            "user": user, "admin": is_admin(user), "model_roles": MODEL_ROLES}
