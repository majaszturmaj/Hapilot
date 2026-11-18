"""Host side of the v3 agent's domain tools: a Unix socket the agent container reaches, one per conversation run.

The container has no network and no copy of the index, the templates or the n8n address. Its MCP server
(agent/mcp/hapilot_mcp.py) forwards three tools here; they run with v2's own code:

  semantic_search  knowledge_base.search (hybrid: E5 + BM25, RRF) over the company index → container paths of each
                   document and its text copy, page or sheet, a short fragment
  pdf_document     agent_tools: the template catalogue (built-in, admin edits, the user's own), the parameter
                   checks, the signature place (src/signatures.py: blank unless the user chose the scan), LaTeX in
                   the v2 sandbox → the PDF is moved into the conversation's /praca/wyniki
  email_draft      a draft through n8n (operations_server's webhook); off unless enabled for the run, and only after
                   an approval recorded here

Approvals: the runner asks before a tool that leaves the container (`approve`); the host's approver decides (the UI's
buttons from stage 4; a fixed policy in tests and dry runs) and the bridge records the exact arguments approved. An
e-mail request is carried out only against such a record, which it uses up. The socket is mounted into the container,
so a shell command could talk to it too: it gets search results (from documents the container can read anyway) and
templates, never an unapproved e-mail.

Protocol: one request per connection, a JSON line {"method", "params"} → a JSON line {"ok", "result" | "error"}.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import socketserver
import threading
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

from src.agent_workspace import Workspace, _safe_name, document_path, text_path
from src.config import get_settings

NEEDS_APPROVAL = frozenset({"email_draft"})
MAX_REQUEST = 1024 * 1024
SNIPPET_CHARS = 400
MAX_K = 15
EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")

Approver = Callable[[str, dict], tuple[bool, str]]


class BridgeError(Exception):
    """Refused or failed request; the message reaches the model."""


def deny_all(tool: str, _input: dict) -> tuple[bool, str]:
    return False, "brak zgody użytkownika (uruchomienie bez użytkownika)"


def approval_key(tool: str, args: dict) -> str:
    canonical = json.dumps({"tool": tool, "args": args}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _text(value, field: str, limit: int, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise BridgeError(f"pole {field}: wymagany tekst")
    if len(value) > limit:
        raise BridgeError(f"pole {field}: za długie (limit {limit} znaków)")
    return value


class Bridge:
    def __init__(self, workspace: Workspace, *, user: Optional[str] = None, signature: str = "blank",
                 email: bool = False, approver: Optional[Approver] = None, trace_path: Optional[Path] = None):
        self.workspace = workspace
        self.user = user
        self.signature = signature if signature in ("blank", "scan") else "blank"
        self.email = email
        self.approver = approver or deny_all
        self.trace_path = Path(trace_path) if trace_path else None
        self._approved: Counter[str] = Counter()
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- dispatch

    def handle(self, method: str, params: dict) -> object:
        started = time.monotonic()
        ok, note = True, None
        try:
            if not isinstance(params, dict):
                raise BridgeError("params muszą być obiektem JSON")
            fn = {"semantic_search": self.semantic_search, "pdf_document": self.pdf_document,
                  "email_draft": self.email_draft, "approve": self.approve}.get(method)
            if fn is None:
                raise BridgeError(f"nieznana metoda: {method}")
            return fn(params)
        except BridgeError as exc:
            ok, note = False, str(exc)
            raise
        except Exception as exc:
            ok, note = False, f"{exc.__class__.__name__}: {exc}"
            raise BridgeError(f"błąd narzędzia {method} ({exc.__class__.__name__})") from exc
        finally:
            self._trace(method, params, ok, note, time.monotonic() - started)

    def _trace(self, method: str, params, ok: bool, note, seconds: float) -> None:
        if not self.trace_path:
            return
        entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "method": method, "ok": ok, "ms": round(seconds * 1000)}
        if isinstance(params, dict):
            if method == "semantic_search":
                entry["query"] = str(params.get("query", ""))[:200]
            elif method == "pdf_document":
                entry["action"], entry["template_id"] = params.get("action"), params.get("template_id")
            elif method in ("email_draft", "approve"):  # no addresses or text in the trace
                entry["tool"] = params.get("tool")
        if note:
            entry["note"] = note[:500]
        with self._lock, open(self.trace_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ----------------------------------------------------------------- approvals

    def approve(self, params: dict) -> dict:
        tool, args = params.get("tool"), params.get("input")
        if not isinstance(tool, str) or not isinstance(args, dict):
            raise BridgeError("approve: tool i input są wymagane")
        if tool not in NEEDS_APPROVAL:
            return {"approved": True, "reason": None}
        approved, reason = self.approver(tool, args)
        if approved:
            with self._lock:
                self._approved[approval_key(tool, args)] += 1
        return {"approved": bool(approved), "reason": reason}

    def _use_approval(self, tool: str, args: dict) -> None:
        key = approval_key(tool, args)
        with self._lock:
            if self._approved[key] <= 0:
                raise BridgeError("ta czynność wymaga zgody użytkownika, której nie udzielono")
            self._approved[key] -= 1

    # ----------------------------------------------------------------- tools

    def semantic_search(self, params: dict) -> str:
        from src import knowledge_base

        query = _text(params.get("query"), "query", 2000)
        k = params.get("k", 8)
        if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= MAX_K:
            raise BridgeError(f"pole k: liczba od 1 do {MAX_K}")
        s = get_settings()
        chunks = knowledge_base.search(query, k=k, targets=[(Path(s.chroma_path), s.collection)])
        if not chunks:
            return "Brak wyników."
        lines = []
        for c in chunks:
            where = f", str. {c.page}" if c.page else (f", arkusz {c.sheet}" if c.sheet else "")
            snippet = " ".join(c.text.split())
            if len(snippet) > SNIPPET_CHARS:
                snippet = snippet[:SNIPPET_CHARS].rsplit(" ", 1)[0] + " …"
            lines.append(f"{c.rank}. {document_path(c.file)}{where}\n   tekst: {text_path(c.file)}\n   „{snippet}”")
        return "\n".join(lines)

    def pdf_document(self, params: dict) -> str:
        from src import agent_tools, run_context

        action = params.get("action")
        if action not in ("list", "create"):
            raise BridgeError('pole action: "list" albo "create"')
        work = self.workspace.dir / "pdf"
        with run_context.use_context(user=self.user, workspace_root=str(work), session_id="robocze"):
            if action == "list":
                return agent_tools.tool_list_templates()
            tid = _text(params.get("template_id"), "template_id", 64)
            values = params.get("parameters") or {}
            if not isinstance(values, dict):
                raise BridgeError("pole parameters: obiekt nazwa → tekst")
            values = {str(k): ("" if v is None else str(v)) for k, v in values.items()}
            if sum(len(v) for v in values.values()) > 100_000:
                raise BridgeError("pole parameters: za długie")
            folder = run_context.workspace_dir()
            before = set(folder.glob("*.pdf"))
            result = agent_tools.tool_generate_pdf_from_template(tid, values, signature=self.signature)
            made = sorted(set(folder.glob("*.pdf")) - before, key=lambda p: p.stat().st_mtime)
        if not result.startswith("SUKCES") or not made:
            return result  # the v2 message: unknown template, missing parameters, LaTeX errors
        target = self._result_name(params.get("file_name"), tid)
        self.workspace.results_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(made[-1]), target)
        return f"SUKCES: zapisano /praca/wyniki/{target.name} ({target.stat().st_size} bajtów)"

    def _result_name(self, wanted, template_id: str) -> Path:
        name = _safe_name(wanted) if isinstance(wanted, str) and wanted.strip() else f"{template_id}.pdf"
        stem = name[:-4] if name.lower().endswith(".pdf") else name
        stem = stem[:100] or template_id
        candidate, n = self.workspace.results_dir / f"{stem}.pdf", 1
        while candidate.exists() or candidate.is_symlink():
            candidate, n = self.workspace.results_dir / f"{stem}_{n}.pdf", n + 1
        return candidate

    def email_draft(self, params: dict) -> str:
        if not self.email:
            raise BridgeError("tworzenie szkiców e-mail jest wyłączone")
        to = _text(params.get("to"), "to", 2000)
        subject = _text(params.get("subject"), "subject", 500)
        body = _text(params.get("body"), "body", 50_000)
        addresses = [a.strip() for a in re.split(r"[,;]", to) if a.strip()]
        if not addresses or len(addresses) > 20 or not all(EMAIL.match(a) for a in addresses):
            raise BridgeError("pole to: adresy e-mail oddzielone przecinkami")
        self._use_approval("email_draft", params)
        import httpx

        from src.config import assert_local_url

        base = get_settings().n8n_webhook_base
        assert_local_url(base)
        try:
            response = httpx.post(f"{base}/outlook-draft", json={"to": ", ".join(addresses), "subject": subject,
                                                                 "body": body}, timeout=15.0)
            response.raise_for_status()
            draft_id = (response.json() or {}).get("draft_id")
        except Exception as exc:
            raise BridgeError(f"n8n nie utworzył szkicu ({exc.__class__.__name__})") from None
        return f"SUKCES: utworzono szkic e-maila w skrzynce użytkownika (nie wysłano). ID: {draft_id}"

    # ----------------------------------------------------------------- socket

    @contextmanager
    def serve(self, socket_path: Path) -> Iterator[Path]:
        """Listen on ``socket_path`` (mode 0600) in a background thread while the block runs."""
        socket_path = Path(socket_path)
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        socket_path.unlink(missing_ok=True)
        bridge = self

        class Handler(socketserver.StreamRequestHandler):
            timeout = 600

            def handle(self):
                line = self.rfile.readline(MAX_REQUEST + 1)
                try:
                    if len(line) > MAX_REQUEST:
                        raise BridgeError("żądanie jest za duże")
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise BridgeError("żądanie musi być obiektem JSON")
                    reply = {"ok": True, "result": bridge.handle(str(request.get("method")),
                                                                 request.get("params") or {})}
                except json.JSONDecodeError:
                    reply = {"ok": False, "error": "niepoprawny JSON"}
                except BridgeError as exc:
                    reply = {"ok": False, "error": str(exc)}
                self.wfile.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        server = Server(str(socket_path), Handler)
        socket_path.chmod(0o600)
        thread = threading.Thread(target=server.serve_forever, name="hapilot-bridge", daemon=True)
        thread.start()
        try:
            yield socket_path
        finally:
            server.shutdown()
            server.server_close()
            socket_path.unlink(missing_ok=True)
