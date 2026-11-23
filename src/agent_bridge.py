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

Questions (stage 4): the agent's ask_question comes here (`ask`) and the host's asker puts it to the user (the UI);
without one the answer is the first option, as the Cline CLI answers without a terminal. A shell command could ask
too; the user sees it like any question of the agent.

Evaluation runs (stage 5, src/eval/agent.py): the index searched is the run's own corpus (``search_targets``), every
search keeps its chunks for the run's trace (``record_chunks``), and the agent's final answer is checked here
(``answer``): the runner sends the JSON answer when the agent calls submit_and_exit, and an answer that is not a JSON
object with the task's fields (and its schema, if given) is refused once with the reason, so the agent can correct it.

Protocol: one request per connection, a JSON line {"method", "params"} → a JSON line {"ok", "result" | "error"}.
"""

from __future__ import annotations

import hashlib
import json
import re
import socketserver
import threading
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

from src.agent_workspace import Workspace, document_path, text_path
from src.config import get_settings

NEEDS_APPROVAL = frozenset({"email_draft"})
MAX_REQUEST = 1024 * 1024
SNIPPET_CHARS = 400
MAX_K = 15
EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")

Approver = Callable[[str, dict], tuple[bool, str]]
Asker = Callable[[str, list], str]
MAX_QUESTION, MAX_OPTIONS, MAX_OPTION = 4000, 10, 500


class BridgeError(Exception):
    """Refused or failed request; the message reaches the model."""


def deny_all(tool: str, _input: dict) -> tuple[bool, str]:
    return False, "brak zgody użytkownika (uruchomienie bez użytkownika)"


def first_option(_question: str, options: list) -> str:
    return options[0] if options else ""


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
                 email: bool = False, approver: Optional[Approver] = None, asker: Optional[Asker] = None,
                 trace_path: Optional[Path] = None, search_targets: Optional[list] = None,
                 answer_format: Optional[dict] = None, record_chunks: bool = False, pdf_drafts: bool = False):
        self.workspace = workspace
        self.user = user
        self.signature = signature if signature in ("blank", "scan") else "blank"
        self.email = email
        self.approver = approver or deny_all
        self.asker = asker or first_option
        self.trace_path = Path(trace_path) if trace_path else None
        self.search_targets = search_targets
        self.answer_format = answer_format
        self.record_chunks = record_chunks
        self.pdf_drafts = pdf_drafts  # chat: "create" saves a draft for the user's form (src/agent_documents.py)
        self.drafts: list[dict] = []  # with pdf_drafts: {"id", "template_id"} per draft of this run
        self.retrievals: list[dict] = []  # with record_chunks: {"query", "k", "chunks", "ms"} per search
        self.answers: list[dict] = []  # every answer check: {"valid", "error", "json"}
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
                  "email_draft": self.email_draft, "approve": self.approve, "ask": self.ask,
                  "answer": self.answer}.get(method)
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
            elif method == "ask":  # nor the question
                entry["options"] = len(params.get("options") or [])
            elif method == "answer" and self.answers:
                entry["valid"], entry["error"] = self.answers[-1]["valid"], self.answers[-1]["error"]
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

    def ask(self, params: dict) -> dict:
        question = _text(params.get("question"), "question", MAX_QUESTION)
        options = params.get("options") or []
        if not isinstance(options, list) or len(options) > MAX_OPTIONS:
            raise BridgeError(f"pole options: lista do {MAX_OPTIONS} odpowiedzi")
        options = [_text(o, "options", MAX_OPTION) for o in options]
        return {"answer": str(self.asker(question, options) or "")}

    def answer(self, params: dict) -> dict:
        """Check the agent's final answer against the task's format: {"valid", "error", "json"}."""
        from src.core.tools_common import extract_json_object
        from src.eval.formatter import check

        text = _text(params.get("text"), "text", 200_000, required=False)
        if self.answer_format is None:
            verdict = {"valid": True, "error": None, "json": None}
        else:
            obj = extract_json_object(text) if text.strip() else None
            error = check(obj, self.answer_format)
            verdict = {"valid": error is None, "error": error, "json": obj if isinstance(obj, dict) else None}
        with self._lock:
            self.answers.append(verdict)
        return verdict

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
        started = time.monotonic()
        chunks = knowledge_base.search(query, k=k, targets=self.search_targets or [(Path(s.chroma_path), s.collection)])
        if self.record_chunks:
            with self._lock:
                self.retrievals.append({"query": query, "k": k, "ms": round((time.monotonic() - started) * 1000),
                                        "chunks": [{"rank": c.rank, "file": c.file, "chunk_id": c.chunk_id,
                                                    "score": c.score, "page": c.page, "text": c.text} for c in chunks]})
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
        from src import agent_documents, agent_tools, run_context

        action = params.get("action")
        if action not in ("list", "create"):
            raise BridgeError('pole action: "list" albo "create"')
        if action == "list":
            work = self.workspace.dir / "pdf"
            with run_context.use_context(user=self.user, workspace_root=str(work), session_id="robocze"):
                return agent_tools.tool_list_templates()
        tid = _text(params.get("template_id"), "template_id", 64)
        values = params.get("parameters") or {}
        if not isinstance(values, dict):
            raise BridgeError("pole parameters: obiekt nazwa → tekst")
        values = {str(k): ("" if v is None else str(v)) for k, v in values.items()}
        if sum(len(v) for v in values.values()) > 100_000:
            raise BridgeError("pole parameters: za długie")
        if self.pdf_drafts:  # the chat: the user checks the values in the form first, then the PDF is made
            unknown = agent_documents.unknown_template(self.user, tid)
            if unknown:
                return unknown
            values.pop("_signature", None)  # the signature is the user's choice in the form, never the model's
            doc_id = agent_documents.save_draft(self.workspace, tid, values, params.get("file_name"))
            with self._lock:
                self.drafts.append({"id": doc_id, "template_id": tid})
            return agent_documents.DRAFT_NOTE.format(template_id=tid)
        target, result = agent_documents.make_pdf(self.workspace, self.user, tid, values, self.signature,
                                                  params.get("file_name"), keep=False)
        return result  # SUKCES with the path, or the v2 message: unknown template, missing parameters, LaTeX errors

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
