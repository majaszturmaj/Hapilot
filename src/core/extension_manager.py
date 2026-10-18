"""
ExtensionManager — Discovers, validates, and loads Hapilot extensions.

This is the heart of the plug-and-play system. It:
1. Scans the extensions/ directory for hapilot_extension.json manifests
2. Validates manifests against the expected schema
3. Imports Python modules from extension directories
4. Registers routes, nodes, tools, and prompts into central registries
5. Provides lifecycle hooks (on_load, on_unload)
6. Builds the dynamic LangGraph workflow from all loaded extensions

Usage:
    from src.core.extension_manager import extension_manager
    extension_manager.discover_and_load()    # Call once at startup
    graph = extension_manager.build_graph()  # Get the compiled LangGraph
"""

import importlib
import importlib.util
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from src.core.base_extension import BaseExtension
from src.core.prompt_registry import prompt_registry
from src.core.route_registry import RouteDefinition, route_registry


# --- Extension Manifest Dataclass ---

@dataclass
class ToolDefinition:
    """A tool that the planner can reference."""
    name: str
    planner_description: str = ""
    planner_when_to_use: str = ""
    planner_when_not_to_use: str = ""
    extension_id: str = ""


@dataclass
class NodeDefinition:
    """A graph node provided by an extension."""
    name: str
    entry_point: str          # e.g. "nodes:run_coder" (module:function)
    edges: Dict[str, str] = field(default_factory=dict)  # e.g. {"success": "__end__"}
    extension_id: str = ""


@dataclass
class ExtensionManifest:
    """Parsed extension manifest (hapilot_extension.json)."""
    id: str
    name: str
    version: str = "0.1.0"
    description: str = ""
    author: str = ""
    license: str = ""
    enabled: bool = True
    directory: str = ""      # Absolute path to extension directory

    routes: List[dict] = field(default_factory=list)
    nodes: List[dict] = field(default_factory=list)
    tools: List[dict] = field(default_factory=list)
    prompts: Dict[str, str] = field(default_factory=dict)
    config_schema: Dict[str, dict] = field(default_factory=dict)
    dependencies: Dict[str, list] = field(default_factory=dict)

    # UI metadata
    node_label: str = ""
    node_description: str = ""


# --- Extension Manager ---

class ExtensionManager:
    """Singleton manager for all extensions.

    Import the module-level `extension_manager` instance.
    """

    _instance: Optional["ExtensionManager"] = None

    def __new__(cls) -> "ExtensionManager":
        if cls._instance is None:
            inst = super().__new__(cls)
            inst._manifests: Dict[str, ExtensionManifest] = {}
            inst._node_functions: Dict[str, Callable] = {}
            inst._tool_definitions: List[ToolDefinition] = []
            inst._extension_instances: Dict[str, BaseExtension] = {}
            inst._loaded: bool = False
            inst._node_labels: Dict[str, str] = {}
            inst._node_descriptions: Dict[str, str] = {}
            inst._node_edges: Dict[str, Dict[str, str]] = {}
            cls._instance = inst
        return cls._instance

    # ------------------------------------------------------------------ #
    # Discovery & Loading                                                  #
    # ------------------------------------------------------------------ #

    def discover_and_load(self, extensions_dir: str = None) -> None:
        """Discover and load all extensions from the extensions/ directory.

        Args:
            extensions_dir: Path to the extensions directory.
                            Defaults to <project_root>/extensions/
        """
        if self._loaded:
            print("⚠️ [EXT MANAGER] Extensions already loaded, skipping re-discovery")
            return

        if extensions_dir is None:
            project_root = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "..")
            )
            extensions_dir = os.path.join(project_root, "extensions")

        if not os.path.isdir(extensions_dir):
            print(f"⚠️ [EXT MANAGER] Extensions directory not found: {extensions_dir}")
            os.makedirs(extensions_dir, exist_ok=True)
            print(f"📁 [EXT MANAGER] Created empty extensions directory: {extensions_dir}")
            self._loaded = True
            return

        # Scan for extension directories containing manifest
        discovered = []
        for entry in sorted(os.listdir(extensions_dir)):
            ext_dir = os.path.join(extensions_dir, entry)
            manifest_path = None
            for candidate in ["hapilot_extension.json", "extension.json"]:
                cp = os.path.join(ext_dir, candidate)
                if os.path.isfile(cp):
                    manifest_path = cp
                    break

            if os.path.isdir(ext_dir) and manifest_path:
                try:
                    manifest = self._load_manifest(manifest_path, ext_dir)
                    if manifest.enabled:
                        discovered.append(manifest)
                    else:
                        print(f"   ⏸️  [{manifest.id}] Disabled — skipping")
                except Exception as e:
                    print(f"   ❌ [{entry}] Manifest error: {e}")

        print(f"🔍 [EXT MANAGER] Discovered {len(discovered)} extension(s) in {extensions_dir}")

        # Load extensions in order (respecting dependencies would go here)
        for manifest in discovered:
            try:
                self._load_extension(manifest, extensions_dir)
            except Exception as e:
                print(f"   ❌ [{manifest.id}] Load error: {e}")
                import traceback
                traceback.print_exc()

        self._loaded = True
        print(
            f"✅ [EXT MANAGER] Loaded {len(self._manifests)} extension(s): "
            f"{', '.join(self._manifests.keys())}"
        )

    def _load_manifest(self, manifest_path: str, ext_dir: str) -> ExtensionManifest:
        """Parse and validate an extension manifest JSON file."""
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        required_fields = ["id", "name"]
        for field_name in required_fields:
            if field_name not in data:
                raise ValueError(f"Missing required field: {field_name}")

        manifest = ExtensionManifest(
            id=data["id"],
            name=data["name"],
            version=data.get("version", "0.1.0"),
            description=data.get("description", ""),
            author=data.get("author", ""),
            license=data.get("license", ""),
            enabled=data.get("enabled", True),
            directory=os.path.abspath(ext_dir),
            routes=data.get("routes", []),
            nodes=data.get("nodes", []),
            tools=data.get("tools", []),
            prompts=data.get("prompts", {}),
            config_schema=data.get("config_schema", {}),
            dependencies=data.get("dependencies", {}),
            node_label=data.get("node_label", ""),
            node_description=data.get("node_description", ""),
        )
        return manifest

    def _load_extension(self, manifest: ExtensionManifest, extensions_dir: str) -> None:
        """Load a single extension: register its routes, nodes, tools, prompts."""
        ext_id = manifest.id
        ext_dir = manifest.directory

        print(f"   📦 Loading [{ext_id}] v{manifest.version}: {manifest.name}")

        # Add extension directory to sys.path so its modules can be imported
        if ext_dir not in sys.path:
            sys.path.insert(0, ext_dir)

        # 1. Register routes
        for route_data in manifest.routes:
            route = RouteDefinition(
                id=route_data.get("id", ""),
                name=route_data.get("name", ext_id),
                description=route_data.get("description", manifest.description),
                keywords_hint=route_data.get("keywords_hint", []),
                router_example=route_data.get("router_example", ""),
                extension_id=ext_id,
                priority=route_data.get("priority", 0),
            )
            route_registry.register(route)

        # 2. Register nodes (import the actual Python functions)
        for node_data in manifest.nodes:
            node_name = node_data["name"]
            entry_point = node_data.get("entry_point", "")

            if entry_point:
                func = self._import_entry_point(entry_point, ext_dir)
                if func:
                    self._node_functions[node_name] = func
                    print(f"      🔧 Node '{node_name}' → {entry_point}")

            # Store edge definitions for graph construction
            edges = node_data.get("edges", {})
            if edges:
                self._node_edges[node_name] = edges

        # Store UI metadata
        if manifest.node_label:
            for node_data in manifest.nodes:
                self._node_labels[node_data["name"]] = manifest.node_label
        if manifest.node_description:
            for node_data in manifest.nodes:
                self._node_descriptions[node_data["name"]] = manifest.node_description

        # Also check per-node labels
        for node_data in manifest.nodes:
            if "label" in node_data:
                self._node_labels[node_data["name"]] = node_data["label"]
            if "description" in node_data:
                self._node_descriptions[node_data["name"]] = node_data["description"]

        # 3. Register tools
        for tool_data in manifest.tools:
            tool_def = ToolDefinition(
                name=tool_data.get("name", ""),
                planner_description=tool_data.get("planner_description", ""),
                planner_when_to_use=tool_data.get("planner_when_to_use", ""),
                planner_when_not_to_use=tool_data.get("planner_when_not_to_use", ""),
                extension_id=ext_id,
            )
            self._tool_definitions.append(tool_def)
            print(f"      🔧 Tool '{tool_def.name}'")

        # 4. Register prompts
        for role, prompt_source in manifest.prompts.items():
            if prompt_source.endswith(".txt"):
                # Load from file
                prompt_path = os.path.join(ext_dir, prompt_source)
                if os.path.isfile(prompt_path):
                    with open(prompt_path, "r", encoding="utf-8") as f:
                        prompt_text = f.read()
                    prompt_registry.register(role, prompt_text, source=ext_id)
                else:
                    print(f"      ⚠️ Prompt file not found: {prompt_path}")
            else:
                # Inline prompt string
                prompt_registry.register(role, prompt_source, source=ext_id)

        # 5. Store manifest
        self._manifests[ext_id] = manifest

        # 6. Instantiate extension class if provided
        ext_class_entry = None
        for node_data in manifest.nodes:
            if "extension_class" in node_data:
                ext_class_entry = node_data["extension_class"]
                break

        if ext_class_entry:
            ext_class = self._import_entry_point(ext_class_entry, ext_dir)
            if ext_class and isinstance(ext_class, type) and issubclass(ext_class, BaseExtension):
                config = {}  # Could load from config file
                instance = ext_class(ext_id, config)
                instance.on_load()
                self._extension_instances[ext_id] = instance

    def _import_entry_point(self, entry_point: str, ext_dir: str) -> Optional[Callable]:
        """Import a function or class from an entry point string.

        Format: "module_name:function_name" (e.g. "nodes:run_coder")
        """
        if ":" not in entry_point:
            print(f"      ⚠️ Invalid entry_point format: {entry_point} (expected 'module:name')")
            return None

        module_name, attr_name = entry_point.split(":", 1)

        try:
            # Try importing as a module within the extension directory
            module_path = os.path.join(ext_dir, f"{module_name}.py")
            if os.path.isfile(module_path):
                spec = importlib.util.spec_from_file_location(
                    f"ext_{os.path.basename(ext_dir)}_{module_name}",
                    module_path,
                )
                module = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = module
                spec.loader.exec_module(module)
                return getattr(module, attr_name, None)
            else:
                # Try standard import
                module = importlib.import_module(module_name)
                return getattr(module, attr_name, None)
        except Exception as e:
            print(f"      ❌ Failed to import {entry_point}: {e}")
            import traceback
            traceback.print_exc()
            return None

    # ------------------------------------------------------------------ #
    # Queries                                                               #
    # ------------------------------------------------------------------ #

    def get_all_node_functions(self) -> Dict[str, Callable]:
        """Return all registered node functions: {name: callable}."""
        return dict(self._node_functions)

    def get_all_tools(self) -> List[ToolDefinition]:
        """Return all registered tool definitions."""
        return list(self._tool_definitions)

    def get_all_manifests(self) -> List[ExtensionManifest]:
        """Return all loaded extension manifests."""
        return list(self._manifests.values())

    def get_node_labels(self) -> Dict[str, str]:
        """Return node_name -> UI label mapping."""
        return dict(self._node_labels)

    def get_node_descriptions(self) -> Dict[str, str]:
        """Return node_name -> UI description mapping."""
        return dict(self._node_descriptions)

    def get_node_edges(self) -> Dict[str, Dict[str, str]]:
        """Return node_name -> edges mapping."""
        return dict(self._node_edges)

    def get_planner_tools_fragment(self) -> str:
        """Generate the dynamic tools section for the planner prompt.

        Returns a formatted string listing all registered tools with their
        descriptions, suitable for injection into the planner system prompt.
        """
        lines = []
        for tool in self._tool_definitions:
            line = f"- {tool.name}"
            if tool.planner_when_to_use:
                line += f"\n  KIEDY UŻYWAĆ: {tool.planner_when_to_use}"
            elif tool.planner_description:
                line += f"\n  {tool.planner_description}"
            if tool.planner_when_not_to_use:
                line += f"\n  KIEDY NIE UŻYWAĆ: {tool.planner_when_not_to_use}"
            lines.append(line)
        return "\n".join(lines)

    def get_planner_tool_names(self) -> List[str]:
        """Return list of all tool names known to the planner."""
        return [t.name for t in self._tool_definitions]

    # ------------------------------------------------------------------ #
    # Lifecycle hooks                                                       #
    # ------------------------------------------------------------------ #

    def emit_hook(self, hook_name: str, **kwargs) -> Optional[dict]:
        """Emit a lifecycle hook to all loaded extension instances.

        Args:
            hook_name: Name of the hook method (e.g. "on_before_route")
            **kwargs: Arguments to pass to the hook

        Returns:
            The last non-None result from any extension, or None.
        """
        result = None
        for ext_id, instance in self._extension_instances.items():
            method = getattr(instance, hook_name, None)
            if method and callable(method):
                try:
                    hook_result = method(**kwargs)
                    if hook_result is not None:
                        result = hook_result
                except Exception as e:
                    print(f"⚠️ [EXT MANAGER] Hook {hook_name} error in {ext_id}: {e}")
        return result

    def unload_all(self) -> None:
        """Unload all extensions (call on_unload hooks)."""
        for ext_id, instance in self._extension_instances.items():
            try:
                instance.on_unload()
            except Exception as e:
                print(f"⚠️ [EXT MANAGER] Unload error in {ext_id}: {e}")
        self._extension_instances.clear()

    # ------------------------------------------------------------------ #
    # Graph Building                                                        #
    # ------------------------------------------------------------------ #

    def build_graph(self):
        """Build and compile a LangGraph StateGraph from all loaded extensions.

        Returns:
            Compiled LangGraph application ready for .invoke() or .stream().
        """
        from src.agent_graph import build_dynamic_graph
        return build_dynamic_graph(self)

    def is_loaded(self) -> bool:
        """Check if extensions have been discovered and loaded."""
        return self._loaded

    def reset(self) -> None:
        """Reset the manager (used in testing)."""
        self._manifests.clear()
        self._node_functions.clear()
        self._tool_definitions.clear()
        self._extension_instances.clear()
        self._node_labels.clear()
        self._node_descriptions.clear()
        self._node_edges.clear()
        self._loaded = False
        route_registry.clear()
        prompt_registry.clear()


# Module-level singleton
extension_manager = ExtensionManager()
