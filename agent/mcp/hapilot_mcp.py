#!/usr/bin/env python3
"""Hapilot's domain tools for the v3 agent, as an MCP server over stdio (standard library only).

Runs inside the agent container, started by the runner (agent/runner/run_task.mjs). Four tools:

  semantic_search  hybrid search of the company documents (the v2 index) → paths, pages, short fragments
  pdf_document     the v2 LaTeX templates → a PDF in /praca/wyniki
  working_days     Polish public holidays and working-day arithmetic (computed here, src/working_days.py)
  email_draft      an e-mail draft through n8n; off unless enabled, and only after the user's approval

working_days runs in this process. The other three need the index, the templates and n8n, which live on the host: they
go through the host bridge (src/agent_bridge.py), a Unix socket mounted read-only into the container. The bridge, not
this server, decides what is allowed (an e-mail draft needs an approval the host recorded), so a command that talks to
the socket directly gets nothing a tool call would not.

Environment: HAPILOT_TOOLS (comma-separated; default semantic_search,pdf_document,working_days),
HAPILOT_BRIDGE (socket path; default /hapilot/most.sock).

Protocol: MCP over stdio, newline-delimited JSON-RPC 2.0; methods initialize, ping, tools/list, tools/call.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # working_days.py sits next to this file in the image
try:
    import working_days
except ImportError:  # the repository layout (tests): src/working_days.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    import working_days

VERSION = "0.3.0"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_TOOLS = ("semantic_search", "pdf_document", "working_days")
BRIDGE_TIMEOUT_S = 300
MAX_REPLY = 4 * 1024 * 1024

TOOLS = {
    "semantic_search": {
        "description": (
            "Search the company documents by meaning and keywords (the same index as Hapilot's chat). Returns the "
            "best fragments with the document path, page or sheet, and the path of its full text copy under "
            "/praca/tekst. Use it to find which documents matter, then read those files; quote file name and page."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "what to look for, in Polish, a few words or a question"},
                "k": {"type": "integer", "minimum": 1, "maximum": 15, "description": "number of fragments (default 8)"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    "pdf_document": {
        "description": (
            "Create a PDF from one of Hapilot's document templates (letter, report, protocol and the company's own). "
            "First call with action \"list\" to get the templates and their parameters, then with action \"create\", "
            "the template id and every required parameter. The PDF is saved in /praca/wyniki and shown to the user. "
            "Signature place: as the user chose in Hapilot (blank by default)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "create"]},
                "template_id": {"type": "string", "description": "REQUIRED for create: the id of a template from list"},
                "parameters": {"type": "object", "additionalProperties": {"type": "string"},
                               "description": "for create: parameter name → text, names exactly as in list"},
                "file_name": {"type": "string", "description": "optional PDF file name, without folders"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    "working_days": {"description": working_days.DESCRIPTION, "inputSchema": working_days.SCHEMA},
    "email_draft": {
        "description": (
            "Create an e-mail DRAFT in the user's mailbox (through n8n). Nothing is sent: the user reviews and sends "
            "it. The user must approve each draft first; if they refuse, do not try again without asking."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "recipient addresses, separated by commas"},
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "plain text"},
            },
            "required": ["to", "subject", "body"],
            "additionalProperties": False,
        },
    },
}


class ToolError(Exception):
    """A tool failed in a way the model should read (returned with isError)."""


def enabled_tools() -> list[str]:
    raw = os.environ.get("HAPILOT_TOOLS")
    names = [n.strip() for n in raw.split(",")] if raw is not None else list(DEFAULT_TOOLS)
    return [n for n in TOOLS if n in names]


def bridge_call(method: str, params: dict, path: str | None = None, timeout: float = BRIDGE_TIMEOUT_S) -> str:
    """One request to the host bridge: a JSON line out, a JSON line back."""
    path = path or os.environ.get("HAPILOT_BRIDGE", "/hapilot/most.sock")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            s.sendall((json.dumps({"method": method, "params": params}, ensure_ascii=False) + "\n").encode())
            data = b""
            while not data.endswith(b"\n"):
                part = s.recv(65536)
                if not part:
                    break
                data += part
                if len(data) > MAX_REPLY:
                    raise ToolError("odpowiedź mostu jest za duża")
    except ToolError:
        raise
    except OSError as exc:
        raise ToolError(f"narzędzie niedostępne (brak połączenia z Hapilotem: {exc.__class__.__name__})") from None
    try:
        reply = json.loads(data)
    except json.JSONDecodeError:
        raise ToolError("niepoprawna odpowiedź mostu") from None
    if not reply.get("ok"):
        raise ToolError(str(reply.get("error") or "błąd narzędzia"))
    return str(reply.get("result", ""))


def call_tool(name: str, args: dict) -> str:
    if name not in enabled_tools():
        raise ToolError(f"nieznane lub wyłączone narzędzie: {name}")
    if not isinstance(args, dict):
        raise ToolError("argumenty muszą być obiektem JSON")
    if name == "working_days":
        try:
            return json.dumps(working_days.run(args), ensure_ascii=False, indent=1)
        except working_days.WorkingDaysError as exc:
            raise ToolError(str(exc)) from None
    return bridge_call(name, args)


def handle(message: dict) -> dict | None:
    """The reply to one JSON-RPC message (None for notifications)."""
    method, mid = message.get("method"), message.get("id")
    if mid is None:
        return None  # notifications/initialized, notifications/cancelled, ...
    params = message.get("params") or {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "hapilot", "version": VERSION}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, **TOOLS[n]} for n in enabled_tools()]}
    elif method == "tools/call":
        try:
            text, error = call_tool(str(params.get("name")), params.get("arguments") or {}), False
        except ToolError as exc:
            text, error = f"BŁĄD: {exc}", True
        except Exception as exc:  # a bug here must reach the model as a tool error, not kill the server
            text, error = f"BŁĄD: wewnętrzny błąd narzędzia ({exc.__class__.__name__})", True
        result = {"content": [{"type": "text", "text": text}], "isError": error}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            reply = handle(message) if isinstance(message, dict) else None
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
