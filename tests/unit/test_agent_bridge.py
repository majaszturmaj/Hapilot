"""Host side of the v3 agent's domain tools (src/agent_bridge.py): search, PDF from templates, e-mail drafts and the
approval record that gates them, the socket protocol."""

import json
import socket
import sys
from pathlib import Path

import pytest

from src import knowledge_base
from src.agent_bridge import Bridge, BridgeError, approval_key
from src.agent_workspace import Workspace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent" / "mcp"))
import hapilot_mcp  # noqa: E402

LETTER = {"SENDER_NAME": "Jan Kowalski", "CITY_DATE": "Wrocław, 18 listopada 2026 r.", "SENDER_COMPANY": "Demo Sp. z o.o.",
          "SENDER_ADDRESS": "ul. Przykładowa 1, 50-001 Wrocław", "RECIPIENT_NAME": "Anna Nowak",
          "RECIPIENT_COMPANY": "Przykładowa Sp. z o.o.", "RECIPIENT_ADDRESS": "ul. Zmyślona 7, 50-001 Wrocław",
          "DOCUMENT_TITLE": "Potwierdzenie terminu", "GREETING": "Szanowna Pani,",
          "BODY": "Potwierdzam termin odbioru: 18 listopada 2026 r.", "CLOSING": "Z poważaniem"}
DRAFT = {"to": "anna.nowak@example.com", "subject": "Termin odbioru", "body": "Dzień dobry, potwierdzam termin."}


@pytest.fixture
def ws(tmp_path):
    return Workspace("rozmowa", root=tmp_path / "ws")


def test_search_validates_its_arguments(ws, monkeypatch):
    monkeypatch.setattr(knowledge_base, "search", lambda q, k=None, targets=None: [])
    bridge = Bridge(ws)
    assert bridge.handle("semantic_search", {"query": "x"}) == "Brak wyników."
    for bad in ({}, {"query": "  "}, {"query": "x", "k": 0}, {"query": "x", "k": 99}, {"query": "x", "k": True},
                {"query": "x" * 3000}):
        with pytest.raises(BridgeError):
            bridge.handle("semantic_search", bad)
    with pytest.raises(BridgeError, match="nieznana metoda"):
        bridge.handle("run_commands", {})


def test_pdf_lists_templates_and_saves_the_pdf_in_results(ws, docker):
    bridge = Bridge(ws)
    listed = json.loads(bridge.handle("pdf_document", {"action": "list"}))
    assert {"letter", "report", "protocol"} <= {t["id"] for t in listed}
    result = bridge.handle("pdf_document", {"action": "create", "template_id": "letter", "parameters": LETTER,
                                            "file_name": "../potwierdzenie terminu.pdf"})
    assert result.startswith("SUKCES: zapisano /praca/wyniki/potwierdzenie_terminu.pdf"), result
    assert [r["name"] for r in ws.results()] == ["potwierdzenie_terminu.pdf"]  # only the PDF reaches the results
    again = bridge.handle("pdf_document", {"action": "create", "template_id": "letter", "parameters": LETTER,
                                           "file_name": "potwierdzenie terminu"})
    assert "potwierdzenie_terminu_1.pdf" in again
    tex = list((ws.dir / "pdf").rglob("*.tex"))
    assert tex and "{{PODPIS_SLOT}}" not in tex[0].read_text(encoding="utf-8")  # signature place filled (blank)


def test_pdf_errors_come_back_as_v2_messages(ws, docker):
    bridge = Bridge(ws)
    missing = bridge.handle("pdf_document", {"action": "create", "template_id": "letter", "parameters": {"SENDER_NAME": "Jan"}})
    assert missing.startswith("BŁĄD: Brakujące wymagane parametry") and ws.results() == []
    assert "Nieznany szablon" in bridge.handle("pdf_document", {"action": "create", "template_id": "brak", "parameters": {}})
    with pytest.raises(BridgeError):
        bridge.handle("pdf_document", {"action": "delete"})
    with pytest.raises(BridgeError):
        bridge.handle("pdf_document", {"action": "create", "template_id": "letter", "parameters": "BODY=x"})


def test_signature_scan_cannot_be_chosen_by_the_model(ws, docker):
    bridge = Bridge(ws)
    with_scan = {**LETTER, "_signature": "scan"}
    assert bridge.handle("pdf_document", {"action": "create", "template_id": "letter", "parameters": with_scan}).startswith("SUKCES")
    params = list((ws.dir / "pdf").rglob("*.params.json"))
    assert json.loads(params[0].read_text(encoding="utf-8"))["signature"] == "blank"


@pytest.fixture
def n8n(monkeypatch):
    import httpx

    sent = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"draft_id": "d-1"}

    def post(url, json=None, timeout=None):
        sent.append((url, json))
        return Response()

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setenv("HAPILOT_N8N_WEBHOOK_BASE", "http://127.0.0.1:8678/webhook")
    return sent


def test_email_is_off_by_default(ws, n8n):
    with pytest.raises(BridgeError, match="wyłączone"):
        Bridge(ws, approver=lambda t, a: (True, None)).handle("email_draft", DRAFT)
    assert n8n == []


def test_email_needs_a_recorded_approval_and_uses_it_up(ws, n8n, tmp_path):
    asked = []
    bridge = Bridge(ws, email=True, approver=lambda tool, args: asked.append((tool, args)) or (True, None),
                    trace_path=tmp_path / "bridge.jsonl")
    with pytest.raises(BridgeError, match="wymaga zgody"):
        bridge.handle("email_draft", DRAFT)  # e.g. a shell command talking to the socket
    assert bridge.handle("approve", {"tool": "email_draft", "input": DRAFT, "call_id": "c1"}) == {"approved": True, "reason": None}
    assert asked == [("email_draft", DRAFT)]
    with pytest.raises(BridgeError, match="wymaga zgody"):
        bridge.handle("email_draft", {**DRAFT, "to": "inny@example.com"})  # not what was approved
    assert "nie wysłano" in bridge.handle("email_draft", dict(reversed(list(DRAFT.items()))))  # key order does not matter
    assert n8n == [("http://127.0.0.1:8678/webhook/outlook-draft", DRAFT)]
    with pytest.raises(BridgeError, match="wymaga zgody"):
        bridge.handle("email_draft", DRAFT)  # one approval, one draft
    trace = (tmp_path / "bridge.jsonl").read_text(encoding="utf-8")
    assert "example.com" not in trace and "potwierdzam" not in trace


def test_refused_approval_records_nothing(ws, n8n):
    bridge = Bridge(ws, email=True)  # default approver: nobody to ask → refused
    answer = bridge.handle("approve", {"tool": "email_draft", "input": DRAFT})
    assert answer["approved"] is False and answer["reason"]
    with pytest.raises(BridgeError, match="wymaga zgody"):
        bridge.handle("email_draft", DRAFT)
    assert n8n == []
    assert bridge.handle("approve", {"tool": "semantic_search", "input": {"query": "x"}})["approved"] is True


def test_bad_addresses_fail_before_the_approval_is_used(ws, n8n):
    bad = {**DRAFT, "to": "anna.nowak"}
    bridge = Bridge(ws, email=True, approver=lambda t, a: (True, None))
    bridge.handle("approve", {"tool": "email_draft", "input": bad})
    with pytest.raises(BridgeError, match="adresy"):
        bridge.handle("email_draft", bad)
    assert bridge._approved[approval_key("email_draft", bad)] == 1 and n8n == []


def test_email_to_a_non_local_n8n_is_refused(ws, n8n, monkeypatch):
    monkeypatch.setenv("HAPILOT_N8N_WEBHOOK_BASE", "https://n8n.example.com/webhook")
    bridge = Bridge(ws, email=True, approver=lambda t, a: (True, None))
    bridge.handle("approve", {"tool": "email_draft", "input": DRAFT})
    with pytest.raises(Exception):
        bridge.handle("email_draft", DRAFT)
    assert n8n == []


def test_socket_protocol(ws, tmp_path, monkeypatch):
    monkeypatch.setattr(knowledge_base, "search", lambda q, k=None, targets=None: [])
    with Bridge(ws).serve(tmp_path / "most.sock") as sock:
        assert oct(sock.stat().st_mode & 0o777) == "0o600"
        assert hapilot_mcp.bridge_call("semantic_search", {"query": "x"}, path=str(sock)) == "Brak wyników."
        with pytest.raises(hapilot_mcp.ToolError, match="nieznana metoda"):
            hapilot_mcp.bridge_call("delete_everything", {}, path=str(sock))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(str(sock))
            s.sendall(b"not json\n")
            assert json.loads(s.makefile().readline()) == {"ok": False, "error": "niepoprawny JSON"}
    assert not sock.exists()
