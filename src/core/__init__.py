"""
Hapilot Core — Lightweight extension system for plug-and-play AI agent capabilities.

This package provides the core abstractions for building and loading extensions:
- ExtensionManager: discovers and loads extensions from the extensions/ directory
- BaseExtension: optional base class with lifecycle hooks
- PromptRegistry: centralized prompt management with extension overrides
- RouteRegistry: dynamic route registration for the LLM router
"""
