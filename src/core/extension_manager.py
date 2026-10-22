"""ExtensionManager — discovers, validates and loads Hapilot extensions (manifest version 1).

An extension is a folder with ``hapilot_extension.json`` (schema: docs/schemas/extension.schema.json).
The manager
1. finds extensions in ``extensions/`` and in the folders listed in ``HAPILOT_EXTENSIONS_DIRS``;
2. validates each manifest against the schema and checks names (no duplicate extensions, nodes or tools;
   ``then`` / ``retry_to`` / ``tool.node`` must point at existing nodes) — an invalid extension is skipped
   as a whole and the reason is recorded in ``errors``;
3. imports node functions and the optional hooks class, registers prompts;
4. provides the planner's tool catalog, the tool → node map and the node edge rules to the graph builder.

Check extensions without starting Hapilot:  ``python -m src.core.extension_manager [DIR ...]``
"""

from __future__ import annotations

import atexit
import importlib.util
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from src.core.base_extension import BaseExtension
from src.core.prompt_registry import prompt_registry

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = "hapilot_extension.json"
SCHEMA_FILE = PROJECT_ROOT / "docs" / "schemas" / "extension.schema.json"
CORE_PROMPTS = Path(__file__).resolve().parent / "prompts"


class ExtensionError(RuntimeError):
    pass


@dataclass
class ToolSpec:
    """A tool the planner can put into a plan step; steps with this tool go to ``node``."""

    name: str
    node: str
    extension_id: str
    description: str
    when_to_use: str = ""
    when_not_to_use: str = ""
    example: str = ""
    aliases: list[str] = field(default_factory=list)


@dataclass
class NodeSpec:
    name: str
    extension_id: str
    func: Callable
    label: str = ""
    description: str = ""
    then: Optional[str] = None
    after: dict = field(default_factory=lambda: {"policy": "plan"})


@dataclass
class ExtensionManifest:
    id: str
    name: str
    version: str
    description: str
    author: str
    license: str
    enabled: bool
    directory: str
    raw: dict

    @property
    def nodes(self) -> list[dict]:  # kept for callers that read manifest.nodes
        return self.raw.get("nodes", [])


# Tools provided by the core graph itself
CORE_TOOLS = [
    ToolSpec(name="hitl_confirm", node="hitl_confirm", extension_id="core",
             description="Zatrzymanie i prośba do użytkownika o potwierdzenie przed ważnym krokiem",
             when_to_use="Przed krokiem, który użytkownik powinien zatwierdzić (np. utworzenie wersji roboczej e-maila)",
             when_not_to_use="Przed template_pdf — parametry dokumentu i tak są pokazywane do potwierdzenia"),
]


def default_dirs() -> list[Path]:
    dirs = [PROJECT_ROOT / "extensions"]
    for part in os.environ.get("HAPILOT_EXTENSIONS_DIRS", "").split(os.pathsep):
        if part.strip():
            dirs.append(Path(part.strip()).expanduser())
    return dirs


def _schema() -> dict:
    return json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))


def validate_manifest(data: Any) -> list[str]:
    """Schema problems of a manifest (empty list = valid)."""
    import jsonschema

    validator = jsonschema.Draft202012Validator(_schema())
    return [f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}" for e in validator.iter_errors(data)]


class ExtensionManager:
    def __init__(self, dirs: Optional[list[Path]] = None, extra: Optional[list[Path]] = None):
        """``dirs``: folders that contain extension folders (default: extensions/ + HAPILOT_EXTENSIONS_DIRS);
        ``extra``: individual extension folders loaded as well (a user's custom nodes, src/admin.py)."""
        self.dirs = [Path(d) for d in dirs] if dirs is not None else None
        self.extra = [Path(d) for d in (extra or [])]
        self.reset()

    # ------------------------------------------------------------------ loading
    def reset(self) -> None:
        self._manifests: dict[str, ExtensionManifest] = {}
        self._nodes: dict[str, NodeSpec] = {}
        self._tools: dict[str, ToolSpec] = {}
        self._hooks: dict[str, BaseExtension] = {}
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self._loaded = False

    def is_loaded(self) -> bool:
        return self._loaded

    def discover_and_load(self) -> None:
        if self._loaded:
            return
        self._register_core_prompts()
        candidates = []
        for directory in (self.dirs if self.dirs is not None else default_dirs()):
            if not directory.is_dir():
                self.warnings.append(f"extensions folder not found: {directory}")
                continue
            for ext_dir in sorted(p for p in directory.iterdir() if (p / MANIFEST).is_file()):
                manifest = self._read_manifest(ext_dir)
                if manifest is not None and manifest.enabled:
                    candidates.append(manifest)
                elif manifest is not None:
                    print(f"   ⏸️  [{manifest.id}] disabled — skipping")
        for ext_dir in self.extra:
            manifest = self._read_manifest(ext_dir) if (ext_dir / MANIFEST).is_file() else None
            if manifest is not None and manifest.enabled:
                candidates.append(manifest)
        staged = [s for s in (self._stage(m) for m in candidates) if s is not None]
        for item in self._check_links(staged):
            self._commit(*item)
        self._loaded = True
        for problem in self.errors:
            print(f"   ❌ [EXT MANAGER] {problem}")
        print(f"✅ [EXT MANAGER] Loaded {len(self._manifests)} extension(s): {', '.join(self._manifests)}")

    def _register_core_prompts(self) -> None:
        for path in sorted(CORE_PROMPTS.glob("*.txt")):
            prompt_registry.register(path.stem, path.read_text(encoding="utf-8"), source="core")

    def _read_manifest(self, ext_dir: Path) -> Optional[ExtensionManifest]:
        try:
            data = json.loads((ext_dir / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.errors.append(f"{ext_dir.name}: cannot read {MANIFEST}: {exc}")
            return None
        problems = validate_manifest(data)
        if problems:
            self.errors.append(f"{ext_dir.name}: invalid manifest — " + "; ".join(problems[:5]))
            return None
        if "routes" in data:
            self.warnings.append(f"{data['id']}: 'routes' is ignored (the router follows the plan)")
        return ExtensionManifest(id=data["id"], name=data["name"], version=data.get("version", "0.0.0"),
                                 description=data.get("description", ""), author=data.get("author", ""),
                                 license=data.get("license", ""), enabled=data.get("enabled", True),
                                 directory=str(ext_dir.resolve()), raw=data)

    def _import(self, ext_dir: Path, entry_point: str, ext_id: str) -> Any:
        module_name, attr = entry_point.split(":", 1)
        path = ext_dir / f"{module_name}.py"
        if not path.is_file():
            raise ExtensionError(f"{entry_point}: {path.name} not found")
        if str(ext_dir) not in sys.path:
            sys.path.insert(0, str(ext_dir))  # lets the extension import its own helper modules
        qualified = f"hapilot_ext_{ext_id}_{module_name}"
        module = sys.modules.get(qualified)
        if module is None:
            spec = importlib.util.spec_from_file_location(qualified, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[qualified] = module
            try:
                spec.loader.exec_module(module)
            except Exception as exc:
                sys.modules.pop(qualified, None)
                raise ExtensionError(f"{entry_point}: import failed: {type(exc).__name__}: {exc}") from exc
        if not hasattr(module, attr):
            raise ExtensionError(f"{entry_point}: {module_name}.py has no {attr!r}")
        return getattr(module, attr)

    def _stage(self, m: ExtensionManifest):
        """Import everything an extension needs; nothing is registered yet."""
        ext_dir = Path(m.directory)
        try:
            if m.id in self._manifests:
                raise ExtensionError("an extension with this id is already loaded")
            nodes = []
            for n in m.raw.get("nodes", []):
                func = self._import(ext_dir, n["entry_point"], m.id)
                if not callable(func):
                    raise ExtensionError(f"{n['entry_point']} is not callable")
                nodes.append(NodeSpec(name=n["name"], extension_id=m.id, func=func, label=n.get("label", ""),
                                      description=n.get("description", ""), then=n.get("then"),
                                      after=n.get("after", {"policy": "plan"})))
            node_names = {n.name for n in nodes}
            tools = []
            for t in m.raw.get("tools", []):
                target = t.get("node") or (nodes[0].name if nodes else None)
                if target is None or target not in node_names:
                    raise ExtensionError(f"tool {t['name']!r} needs a node of this extension (got {target!r})")
                tools.append(ToolSpec(name=t["name"], node=target, extension_id=m.id, description=t["description"],
                                      when_to_use=t.get("when_to_use", ""), when_not_to_use=t.get("when_not_to_use", ""),
                                      example=t.get("example", ""), aliases=list(t.get("aliases", []))))
            prompts = {}
            for role, source in m.raw.get("prompts", {}).items():
                if source.endswith(".txt"):
                    path = ext_dir / source
                    if not path.is_file():
                        raise ExtensionError(f"prompt file {source} not found")
                    prompts[role] = path.read_text(encoding="utf-8")
                else:
                    prompts[role] = source
            hooks = None
            if m.raw.get("hooks"):
                cls = self._import(ext_dir, m.raw["hooks"], m.id)
                if not (isinstance(cls, type) and issubclass(cls, BaseExtension)):
                    raise ExtensionError(f"hooks {m.raw['hooks']} must be a subclass of BaseExtension")
                hooks = cls(m.id, dict(m.raw.get("config", {})))
            return m, nodes, tools, prompts, hooks
        except ExtensionError as exc:
            self.errors.append(f"{m.id}: {exc}")
            return None

    def _check_links(self, staged: list) -> list:
        """Drop extensions whose node/tool names collide or whose edges point at missing nodes."""
        ok, nodes, tools = [], {"task_planner", "router", "hitl_confirm"}, {t.name for t in CORE_TOOLS}
        for item in staged:
            m, ext_nodes, ext_tools = item[0], item[1], item[2]
            clash = [n.name for n in ext_nodes if n.name in nodes] + [t.name for t in ext_tools if t.name in tools]
            if clash:
                self.errors.append(f"{m.id}: names already used by another extension or the core: {', '.join(clash)}")
                continue
            nodes.update(n.name for n in ext_nodes)
            tools.update(t.name for t in ext_tools)
            ok.append(item)
        result = []
        for item in ok:
            m, ext_nodes = item[0], item[1]
            missing = [x for n in ext_nodes for x in (n.then, n.after.get("retry_to")) if x and x not in nodes]
            if missing:
                self.errors.append(f"{m.id}: edges point at unknown nodes: {', '.join(missing)}")
                continue
            result.append(item)
        return result

    def _commit(self, m, nodes, tools, prompts, hooks) -> None:
        self._manifests[m.id] = m
        for n in nodes:
            self._nodes[n.name] = n
        for t in tools:
            self._tools[t.name] = t
        for role, text in prompts.items():
            prompt_registry.register(role, text, source=m.id)
        if hooks is not None:
            hooks.on_load()
            self._hooks[m.id] = hooks
            atexit.register(hooks.on_unload)
        print(f"   📦 [{m.id}] v{m.version}: nodes {', '.join(n.name for n in nodes) or '—'}; "
              f"tools {', '.join(t.name for t in tools) or '—'}")

    # ------------------------------------------------------------------ queries
    def get_all_manifests(self) -> list[ExtensionManifest]:
        return list(self._manifests.values())

    def get_node_specs(self) -> dict[str, NodeSpec]:
        return dict(self._nodes)

    def get_all_node_functions(self) -> dict[str, Callable]:
        return {name: spec.func for name, spec in self._nodes.items()}

    def get_tools(self) -> list[ToolSpec]:
        """Planner catalog: extension tools (in load order) followed by core tools."""
        return list(self._tools.values()) + CORE_TOOLS

    def get_planner_tool_names(self) -> list[str]:
        return [t.name for t in self.get_tools()]

    def tool_node_map(self) -> dict[str, str]:
        """Tool name (and alias) → node. Real tool names win over aliases; the first alias wins."""
        mapping = {t.name: t.node for t in self.get_tools()}
        for t in self.get_tools():
            for alias in t.aliases:
                mapping.setdefault(alias, t.node)
        return mapping

    def get_node_labels(self) -> dict[str, str]:
        return {n: s.label for n, s in self._nodes.items() if s.label}

    def get_node_descriptions(self) -> dict[str, str]:
        return {n: s.description for n, s in self._nodes.items() if s.description}

    def get_config(self, ext_id: str) -> dict:
        """The ``config`` object of a loaded extension's manifest (empty if none)."""
        m = self._manifests.get(ext_id)
        return dict(m.raw.get("config", {})) if m else {}

    # ------------------------------------------------------------------ hooks
    def before_route(self, state: dict) -> Optional[dict]:
        """Ask hooks before routing; the last non-empty answer wins (it may set ``next_step``)."""
        result = None
        for ext_id, hooks in self._hooks.items():
            try:
                answer = hooks.on_before_route(state)
                if answer:
                    result = answer
            except Exception as exc:
                print(f"⚠️ [EXT MANAGER] on_before_route of {ext_id} failed: {exc}")
        return result

    def after_node(self, node_name: str, state: dict, output: dict) -> dict:
        """Let hooks observe or replace a node's output."""
        for ext_id, hooks in self._hooks.items():
            try:
                changed = hooks.on_after_node(node_name, state, output)
                if changed is not None:
                    output = changed
            except Exception as exc:
                print(f"⚠️ [EXT MANAGER] on_after_node of {ext_id} failed: {exc}")
        return output

    def unload_all(self) -> None:
        for hooks in self._hooks.values():
            try:
                hooks.on_unload()
            except Exception:
                pass
        self._hooks.clear()


# The default manager (extensions/ + HAPILOT_EXTENSIONS_DIRS) and the one the running graph was built with
extension_manager = ExtensionManager()
_active: ExtensionManager = extension_manager


def set_active(manager: ExtensionManager) -> None:
    global _active
    _active = manager


def get_active() -> ExtensionManager:
    """The manager of the current run (``configurable["extension_manager"]``: a user's own agent in the UI),
    else the one the shared graph was built with."""
    try:
        from src import run_context

        manager = run_context.current("extension_manager")
        if isinstance(manager, ExtensionManager):
            return manager
    except Exception:
        pass
    return _active


def main(argv: Optional[list[str]] = None) -> int:
    """Validate and list extensions: ``python -m src.core.extension_manager [DIR ...]``."""
    args = sys.argv[1:] if argv is None else argv
    manager = ExtensionManager([Path(a) for a in args] if args else None)
    manager.discover_and_load()
    for t in manager.get_tools():
        print(f"tool {t.name:16s} → node {t.node:20s} ({t.extension_id})")
    for w in manager.warnings:
        print(f"warning: {w}")
    for e in manager.errors:
        print(f"ERROR: {e}")
    return 1 if manager.errors else 0


if __name__ == "__main__":
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    raise SystemExit(main())
