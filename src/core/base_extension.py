"""
BaseExtension — Optional abstract base class for Hapilot extensions.

Extensions can implement lifecycle hooks by subclassing BaseExtension
and specifying the class path in their hapilot_extension.json manifest.

This is OPTIONAL — simple extensions that only provide nodes/tools/prompts
do not need to subclass this. They just need entry_point functions.
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

        Use this for one-time initialization (DB connections, model loading, etc.).
        """
        pass

    def on_unload(self) -> None:
        """Called when the extension is disabled or the system shuts down.

        Use this for cleanup (close connections, flush caches, etc.).
        """
        pass

    def on_before_route(self, state: dict) -> Optional[dict]:
        """Called before the router classifies the user's message.

        Args:
            state: The current AgentState dict.

        Returns:
            Modified state dict, or None to leave state unchanged.
            Return a dict with 'next_step' to short-circuit routing.
        """
        return None

    def on_after_node(self, node_name: str, state: dict, output: dict) -> Optional[dict]:
        """Called after a node has finished executing.

        Args:
            node_name: Name of the node that just executed.
            state: The current AgentState dict.
            output: The output produced by the node.

        Returns:
            Modified output dict, or None to leave output unchanged.
        """
        return None
