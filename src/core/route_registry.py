"""
RouteRegistry — Dynamic route registration for the LLM router.

Routes are registered by extensions via their hapilot_extension.json manifest.
The router prompt is dynamically composed from all registered routes, so adding
a new extension automatically makes it available to the router without any
core code changes.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class RouteDefinition:
    """A single route that the router can classify messages into."""
    id: str                           # e.g. "A", "B", "F"
    name: str                         # e.g. "document_generator", "jira"
    description: str                  # Human-readable description for the router prompt
    keywords_hint: List[str] = field(default_factory=list)  # Keywords for heuristic pre-filtering
    router_example: str = ""          # Example for the router prompt (e.g. '"Napisz raport" -> A')
    extension_id: str = ""            # ID of the extension that registered this route
    priority: int = 0                 # Higher priority routes are listed first


class RouteRegistry:
    """Central registry for all routes available to the router.

    Singleton — import the module-level `route_registry` instance.
    """

    _instance: Optional["RouteRegistry"] = None

    def __new__(cls) -> "RouteRegistry":
        if cls._instance is None:
            inst = super().__new__(cls)
            inst._routes: Dict[str, RouteDefinition] = {}
            inst._next_letter_ord = ord("A")  # Auto-assign letters if not specified
            cls._instance = inst
        return cls._instance

    def register(self, route: RouteDefinition) -> None:
        """Register a new route. Overwrites if the same ID already exists."""
        if not route.id:
            # Auto-assign the next available letter
            route.id = chr(self._next_letter_ord)
            self._next_letter_ord += 1
        elif route.id.isalpha() and len(route.id) == 1:
            # Track the highest used letter for auto-assignment
            letter_ord = ord(route.id.upper())
            if letter_ord >= self._next_letter_ord:
                self._next_letter_ord = letter_ord + 1

        self._routes[route.id] = route
        print(f"🛤️  [ROUTE REGISTRY] Registered route [{route.id}] → {route.name}: {route.description[:60]}")

    def unregister(self, route_id: str) -> None:
        """Remove a route by its ID."""
        if route_id in self._routes:
            del self._routes[route_id]

    def get_all(self) -> List[RouteDefinition]:
        """Get all registered routes, sorted by priority then by ID."""
        return sorted(
            self._routes.values(),
            key=lambda r: (-r.priority, r.id)
        )

    def get_by_name(self, name: str) -> Optional[RouteDefinition]:
        """Look up a route by its node name (e.g. 'document_generator')."""
        for route in self._routes.values():
            if route.name == name:
                return route
        return None

    def get_by_id(self, route_id: str) -> Optional[RouteDefinition]:
        """Look up a route by its letter ID (e.g. 'A')."""
        return self._routes.get(route_id)

    def build_router_prompt_fragment(self) -> str:
        """Generate the dynamic classification section for the router prompt.

        Returns a string like:
            A) DOCUMENT GENERATOR (PDF reports, letters via LaTeX templates)
            B) KNOWLEDGE / RAG (Company procedures, regulations)
            ...
        """
        lines = []
        for route in self.get_all():
            lines.append(f"{route.id}) {route.description}")
        return "\n".join(lines)

    def build_router_examples_fragment(self) -> str:
        """Generate example lines for the router prompt."""
        lines = []
        for route in self.get_all():
            if route.router_example:
                lines.append(f"- {route.router_example}")
        return "\n".join(lines)

    def build_letter_to_name_map(self) -> Dict[str, str]:
        """Return a mapping from route letter to node name.

        E.g. {"A": "document_generator", "B": "knowledge", "C": "chat"}
        """
        return {route.id: route.name for route in self._routes.values()}

    def get_all_node_names(self) -> List[str]:
        """Return all unique node names referenced by routes."""
        return list(set(r.name for r in self._routes.values()))

    def clear(self) -> None:
        """Remove all routes (used in testing)."""
        self._routes.clear()
        self._next_letter_ord = ord("A")


# Module-level singleton
route_registry = RouteRegistry()
