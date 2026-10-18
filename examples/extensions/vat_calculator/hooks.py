"""Hooks of the example extension: count how often the VAT node ran (shows the hook API)."""

from src.core.base_extension import BaseExtension


class VatHooks(BaseExtension):
    def on_load(self) -> None:
        self.loaded = True
        self.runs = 0

    def on_after_node(self, node_name, state, output):
        if node_name == "vat":
            self.runs += 1
        return None  # keep the node's output unchanged
