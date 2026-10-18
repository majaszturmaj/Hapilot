"""Optional MCP server: Outlook e-mail drafts and calendar through a local n8n (never sends e-mail).

    python -m src.operations_server

Requires the n8n compose profile and HAPILOT_N8N_WEBHOOK_BASE (a local address).
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

from src.config import assert_local_url, get_settings  # noqa: E402

mcp = FastMCP("Hapilot Office Operations (n8n)")


def _url(path: str) -> str:
    base = get_settings().n8n_webhook_base
    assert_local_url(base)
    return f"{base}/{path}"


@mcp.tool()
def create_email_draft(to_recipients: str, subject: str, body_content: str) -> str:
    """Create an e-mail DRAFT in Outlook for the user to review. Nothing is sent."""
    try:
        response = httpx.post(_url("outlook-draft"), json={"to": to_recipients, "subject": subject, "body": body_content}, timeout=10.0)
        response.raise_for_status()
        data = response.json()
        return f"✅ Utworzono wersję roboczą w Outlooku (nie wysłano). ID: {data.get('draft_id')}"
    except Exception as exc:
        return f"❌ Błąd tworzenia wersji roboczej: {exc}"


@mcp.tool()
def get_calendar_events(date: str) -> str:
    """Calendar events for a day (YYYY-MM-DD)."""
    try:
        response = httpx.post(_url("outlook-calendar"), json={"date": date}, timeout=10.0)
        response.raise_for_status()
        events = response.json().get("events", [])
    except Exception as exc:
        return f"❌ Błąd pobierania kalendarza: {exc}"
    if not events:
        return f"📅 Brak wydarzeń w kalendarzu na dzień {date}."
    lines = [f"- {e.get('start')} - {e.get('end')}: {e.get('subject')}" for e in events]
    return f"📅 Wydarzenia na dzień {date}:\n" + "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
