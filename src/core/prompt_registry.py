"""
PromptRegistry — Centralized prompt management with extension overrides.

System prompts are registered by extensions. The core provides generic defaults,
and extensions can override or extend prompts for specific roles.

Prompts can be:
1. Registered as raw strings
2. Loaded from .txt files in extension directories
3. Composed dynamically with route/tool context injection
"""

from typing import Callable, Dict, List, Optional


class PromptRegistry:
    """Central registry for all system prompts used by LLM roles.

    Singleton — import the module-level `prompt_registry` instance.

    Prompt resolution order (highest priority first):
    1. Extension-specific override (last registered wins)
    2. Default prompt registered by core or first extension
    """

    _instance: Optional["PromptRegistry"] = None

    def __new__(cls) -> "PromptRegistry":
        if cls._instance is None:
            inst = super().__new__(cls)
            inst._prompts: Dict[str, str] = {}
            inst._prompt_sources: Dict[str, str] = {}  # role -> extension_id
            inst._prompt_composers: Dict[str, Callable] = {}  # role -> dynamic composer fn
            cls._instance = inst
        return cls._instance

    def register(self, role: str, prompt: str, source: str = "core") -> None:
        """Register a system prompt for a given role.

        Args:
            role: The LLM role (e.g. "router", "knowledge", "chat", "template_filler")
            prompt: The full system prompt text
            source: ID of the extension registering this prompt (for debugging)
        """
        self._prompts[role] = prompt
        self._prompt_sources[role] = source
        print(f"📝 [PROMPT REGISTRY] Registered prompt for role '{role}' (from: {source})")

    def register_composer(self, role: str, composer: Callable[[], str]) -> None:
        """Register a dynamic prompt composer for a role.

        The composer is called every time the prompt is requested, allowing
        dynamic content injection (e.g. current routes, tools, time).

        Args:
            role: The LLM role
            composer: A callable that returns the prompt string
        """
        self._prompt_composers[role] = composer

    def get(self, role: str) -> Optional[str]:
        """Get the prompt for a given role.

        If a dynamic composer is registered, it takes precedence over static prompts.

        Args:
            role: The LLM role

        Returns:
            The system prompt string, or None if no prompt is registered for this role.
        """
        # Dynamic composers take priority
        if role in self._prompt_composers:
            try:
                return self._prompt_composers[role]()
            except Exception as e:
                print(f"⚠️ [PROMPT REGISTRY] Composer error for role '{role}': {e}")
                # Fall through to static prompt

        return self._prompts.get(role)

    def get_or_default(self, role: str, default: str = "") -> str:
        """Get prompt for a role, returning a default if not found."""
        return self.get(role) or default

    def has(self, role: str) -> bool:
        """Check if a prompt is registered for a given role."""
        return role in self._prompts or role in self._prompt_composers

    def get_source(self, role: str) -> str:
        """Get the extension ID that registered a given role's prompt."""
        return self._prompt_sources.get(role, "unknown")

    def list_roles(self) -> List[str]:
        """List all registered prompt roles."""
        roles = set(self._prompts.keys()) | set(self._prompt_composers.keys())
        return sorted(roles)

    def clear(self) -> None:
        """Remove all prompts (used in testing)."""
        self._prompts.clear()
        self._prompt_sources.clear()
        self._prompt_composers.clear()


# Module-level singleton
prompt_registry = PromptRegistry()
