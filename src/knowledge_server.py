"""Optional MCP server: search the Hapilot knowledge base from MCP clients on this machine.

    python -m src.knowledge_server

Uses the same embedded ChromaDB as the UI; run it when the UI is not indexing documents.
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.privacy import apply_offline_env

apply_offline_env()

from mcp.server.fastmcp import FastMCP  # noqa: E402

from src import knowledge_base  # noqa: E402

mcp = FastMCP("Hapilot Knowledge Base")


@mcp.tool()
def search_company_docs(query: str) -> str:
    """Search company documents (procedures, regulations) and return the most relevant fragments with their sources."""
    return knowledge_base.format_chunks(knowledge_base.search(query))


if __name__ == "__main__":
    mcp.run()
