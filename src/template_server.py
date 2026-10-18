"""Optional MCP server: Hapilot's LaTeX document templates for MCP clients on this machine.

    python -m src.template_server

Documents are compiled in the same isolated sandbox as in the UI, in the workspace session "mcp".
"""

import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.privacy import apply_offline_env

apply_offline_env()

from mcp.server.fastmcp import FastMCP  # noqa: E402

from src import run_context  # noqa: E402
from src.agent_tools import _load_templates_metadata, tool_generate_pdf_from_template, tool_list_templates  # noqa: E402

mcp = FastMCP("Hapilot LaTeX Templates")


@mcp.tool()
def list_templates() -> str:
    """List the available document templates (letter, report, protocol) with their parameters."""
    return tool_list_templates()


@mcp.tool()
def get_template_details(template_id: str) -> str:
    """Full metadata of one template."""
    info = _load_templates_metadata().get("templates", {}).get(template_id)
    return json.dumps(info, ensure_ascii=False, indent=2) if info else f"BŁĄD: Nieznany szablon '{template_id}'"


@mcp.tool()
def generate_pdf_from_template(template_id: str, parameters_json: str) -> str:
    """Fill a template with parameters (JSON object) and compile it to PDF."""
    try:
        parameters = json.loads(parameters_json)
    except json.JSONDecodeError as exc:
        return f"BŁĄD: parameters_json nie jest poprawnym JSON: {exc}"
    with run_context.use_context(session_id="mcp"):
        result = tool_generate_pdf_from_template(template_id, parameters)
        if result.startswith("SUKCES"):
            result += f"\n(Katalog: {run_context.workspace_dir()})"
    return result


if __name__ == "__main__":
    mcp.run()
