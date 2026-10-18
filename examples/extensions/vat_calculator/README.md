# Example extension: VAT calculator

A complete, small extension: one node (`vat`), one planner tool (`vat`), hooks and configuration.
It computes Polish VAT deterministically (no language model).

Try it without copying anything into the repository:

```bash
HAPILOT_EXTENSIONS_DIRS=examples/extensions .venv/bin/python -m src.core.extension_manager
```

and run Hapilot with the same variable set. See [docs/extension_development.md](../../../docs/extension_development.md).
