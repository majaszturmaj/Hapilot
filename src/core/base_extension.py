"""
BaseExtension — optional base class for extension hooks.

An extension that needs lifecycle hooks subclasses BaseExtension and names the class in its
manifest: ``"hooks": "hooks:MyHooks"``. The manager creates it with the extension id and the
manifest's ``config`` object. Extensions that only provide nodes, tools and prompts do not need it.

A hook that raises is logged and ignored; it never stops the graph.
"""

from typing import Any, Dict, Optional


class BaseExtension:
    """Optional base class for extensions that need lifecycle management.

    Subclass this to hook into the agent lifecycle:
    - on_load: called when the extension is loaded
    - on_unload: called when the extension is disabled/removed
    - on_before_route: called before every routing decision
    - on_after_node: called after every node execution
    """

    def __init__(self, extension_id: str, config: Dict[str, Any]):
        self.extension_id = extension_id
        self.config = config

    def on_load(self) -> None:
        """Called once when the extension is loaded.

        Use this for one-time initialisation (read config, open local resources).
        """
        pass

    def on_unload(self) -> None:
        """Called when the process exits (registered with atexit).

        Use this for cleanup (close connections, flush caches, etc.).
        """
        pass

    def on_before_route(self, state: dict) -> Optional[dict]:
        """Called before the router maps the current plan step to a node.

        Args:
            state: A copy of the current AgentState dict.

        Returns:
            None to let the router follow the plan, or a state update with ``next_step`` (a node name)
            to send this step elsewhere. An unknown node falls back to ``chat``. With several
            extensions, the last non-empty answer wins.
        """
        return None

    def on_after_node(self, node_name: str, state: dict, output: dict) -> Optional[dict]:
        """Called after every node (of any extension) has returned.

        Args:
            node_name: Name of the node that just ran.
            state: The AgentState the node received.
            output: The state update the node returned.

        Returns:
            A replacement state update, or None to keep the node's output unchanged.
        """
        return None
