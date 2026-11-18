"""The v3 agent's MCP server (agent/mcp/hapilot_mcp.py), spoken to over stdio as the runner's MCP client does, and
forwarding to a host bridge (src/agent_bridge.py) on a real Unix socket."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src import knowledge_base
from src.agent_bridge import Bridge
from src.agent_workspace import Workspace

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "agent" / "mcp" / "hapilot_mcp.py"


class Server:
    """The MCP server in a child process, as the SDK's stdio transport starts it."""

    def __init__(self, env: dict):
        self.proc = subprocess.Popen([sys.executable, "-I", str(SERVER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True, env={**os.environ, **env}, cwd="/")
        self.next_id = 0

    def notify(self, method: str, params=None):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params=None) -> dict:
        self.next_id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method,
                                          "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        reply = json.loads(self.proc.stdout.readline())
        assert reply["id"] == self.next_id
        return reply

    def call(self, name: str, arguments: dict) -> tuple[str, bool]:
        result = self.request("tools/call", {"name": name, "arguments": arguments})["result"]
        return result["content"][0]["text"], result["isError"]

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


@pytest.fixture
def server(tmp_path):
    started = []

    def start(**env):
        s = Server({"HAPILOT_BRIDGE": str(tmp_path / "brak.sock"), **env})
        started.append(s)
        return s

    yield start
    for s in started:
        s.close()


def test_handshake_and_tool_list(server):
    s = server()
    init = s.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "test", "version": "0"}})["result"]
    assert init["protocolVersion"] == "2025-06-18" and init["serverInfo"]["name"] == "hapilot"
    assert "tools" in init["capabilities"]
    s.notify("notifications/initialized")  # no reply; the next answer must belong to the next request
    assert s.request("ping")["result"] == {}
    tools = s.request("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["semantic_search", "pdf_document", "working_days"]  # e-mail off by default
    for t in tools:
        schema = t["inputSchema"]
        assert schema["type"] == "object" and set(schema["required"]) <= set(schema["properties"]), t["name"]
        assert schema.get("additionalProperties") is False and len(t["description"]) > 50
    assert s.request("initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == "2025-06-18"
    assert s.request("resources/list")["error"]["code"] == -32601


def test_tool_list_follows_the_setting(server):
    s = server(HAPILOT_TOOLS="working_days,email_draft")
    assert [t["name"] for t in s.request("tools/list")["result"]["tools"]] == ["working_days", "email_draft"]
    text, error = s.call("semantic_search", {"query": "x"})
    assert error and "wyłączone" in text


def test_working_days_runs_in_the_container(server):
    s = server()
    text, error = s.call("working_days", {"operation": "add_working_days", "date": "2026-11-03", "days": 10})
    assert not error and json.loads(text)["wynik"] == "2026-11-18 (środa)"
    text, error = s.call("working_days", {"operation": "is_working_day", "date": "2026-13-01"})
    assert error and text.startswith("BŁĄD") and "nie jest datą" in text


def test_bridge_tools_without_a_bridge_are_an_error_not_a_crash(server):
    s = server()
    text, error = s.call("semantic_search", {"query": "regulamin"})
    assert error and "niedostępne" in text
    assert s.request("ping")["result"] == {}  # still serving


def test_search_goes_through_the_bridge(server, tmp_path, monkeypatch):
    found = [knowledge_base.Chunk(file="umowy/umowa.pdf", chunk_id="c1", score=0.03, text="Kara umowna  wynosi\n0,2% dziennie.",
                                  page=4, rank=1, collection="hapilot"),
             knowledge_base.Chunk(file="dodane/cennik.xlsx", chunk_id="c2", score=0.02, text="x" * 900, sheet="Ceny",
                                  rank=2, collection="hapilot")]
    calls = []
    monkeypatch.setattr(knowledge_base, "search", lambda q, k=None, targets=None: calls.append((q, k)) or found)
    bridge = Bridge(Workspace("t", root=tmp_path / "ws"), trace_path=tmp_path / "bridge.jsonl")
    with bridge.serve(tmp_path / "s" / "most.sock") as sock:
        s = server(HAPILOT_BRIDGE=str(sock))
        text, error = s.call("semantic_search", {"query": "kara umowna", "k": 5})
    assert not error and calls == [("kara umowna", 5)]
    assert "1. /praca/dokumenty/umowy/umowa.pdf, str. 4\n   tekst: /praca/tekst/umowy/umowa.pdf.txt" in text
    assert "„Kara umowna wynosi 0,2% dziennie.”" in text
    assert "2. /praca/dodane/cennik.xlsx, arkusz Ceny" in text and "tekst: /praca/tekst/dodane/cennik.xlsx.txt" in text
    assert len(text.splitlines()[-1]) < 420  # long fragments are cut
    trace = [json.loads(x) for x in (tmp_path / "bridge.jsonl").read_text().splitlines()]
    assert trace[0]["method"] == "semantic_search" and trace[0]["ok"] and trace[0]["query"] == "kara umowna"
