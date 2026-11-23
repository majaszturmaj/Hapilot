"""Engine v3: PDF documents the user checks before they are made, as under engine v2.

In the chat (``Bridge(pdf_drafts=True)``, src/agent_engine.py) the agent's ``pdf_document create`` does not compile:
the bridge saves a *draft* — the template and the values the agent filled in — and tells the agent that the user will
check it. After the answer the UI (src/app.py) shows the v2 document form (public/elements/hitl_form.jsx) with those
values; "Generuj PDF" compiles the template here, without the model, into /praca/wyniki. The values of every PDF made
this way are kept, so the "✏️ Edytuj dane i wygeneruj ponownie" button under it opens the form again and makes a new
version. Evaluation runs keep compiling at once (src/eval/agent.py builds its bridge without drafts).

Storage: ``<conversation folder>/dokumenty_dane/<id>.json`` = {"template_id", "parameters", "file_name",
"signature"}; a draft's id is ``szkic-<n>``, a made PDF's id is its file name in /praca/wyniki. The folder is outside
/praca, so the agent's container never sees it.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Optional

from src.agent_workspace import Workspace, _safe_name

DATA_DIR = "dokumenty_dane"
_ID = re.compile(r"[\w][\w.\-]{0,150}", re.UNICODE)

DRAFT_NOTE = ("SZKIC: dane dokumentu (szablon {template_id}) czekają na użytkownika w formularzu pod Twoją odpowiedzią. "
              "PDF powstanie dopiero, gdy użytkownik sprawdzi i ewentualnie poprawi dane, a potem kliknie „Generuj "
              "PDF”; plik pojawi się wtedy w /praca/wyniki. Nie wywołuj pdf_document ponownie dla tego dokumentu. "
              "W odpowiedzi napisz krótko, że dane dokumentu są gotowe do sprawdzenia w formularzu poniżej.")


def _folder(ws: Workspace) -> Path:
    return ws.dir / DATA_DIR


def _path(ws: Workspace, doc_id: str) -> Optional[Path]:
    if not isinstance(doc_id, str) or not _ID.fullmatch(doc_id):
        return None
    return _folder(ws) / f"{doc_id}.json"


def _write(ws: Workspace, doc_id: str, data: dict) -> None:
    path = _path(ws, doc_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def load(ws: Workspace, doc_id: str) -> Optional[dict]:
    """A draft or the values of a made PDF; None when unknown or unreadable."""
    path = _path(ws, doc_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path is not None and path.is_file() else None
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("template_id") or not isinstance(data.get("parameters"), dict):
        return None
    return data


def unknown_template(user: Optional[str], template_id: str) -> Optional[str]:
    """The v2 message for a template that does not exist (for this user), else None."""
    from src import agent_tools, run_context

    with run_context.use_context(user=user):
        templates = agent_tools._load_templates_metadata().get("templates") or {}
    if template_id in templates:
        return None
    return f"BŁĄD: Nieznany szablon '{template_id}'. Dostępne szablony: {', '.join(templates)}"


def save_draft(ws: Workspace, template_id: str, parameters: dict, file_name=None) -> str:
    """Keep the agent's values for the form; returns the draft's id."""
    _folder(ws).mkdir(parents=True, exist_ok=True)
    n = 1
    while (_folder(ws) / f"szkic-{n}.json").exists():
        n += 1
    doc_id = f"szkic-{n}"
    _write(ws, doc_id, {"template_id": template_id, "parameters": dict(parameters),
                        "file_name": file_name if isinstance(file_name, str) else None, "signature": None})
    return doc_id


def form(user: Optional[str], data: dict) -> dict:
    """The v2 document form for these values: {"content", "fields", …} (src/agent_tools.build_document_hitl_form)."""
    from src import agent_tools, run_context

    with run_context.use_context(user=user):
        return agent_tools.build_document_hitl_form(data["template_id"], data.get("parameters") or {},
                                                    agent_tools._load_templates_metadata(),
                                                    signature=data.get("signature"))


def result_name(ws: Workspace, wanted, template_id: str) -> Path:
    """A free name in /praca/wyniki: the wanted one (made safe) or the template's, with _1, _2 … when taken."""
    name = _safe_name(wanted) if isinstance(wanted, str) and wanted.strip() else f"{template_id}.pdf"
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    stem = stem[:100] or template_id
    candidate, n = ws.results_dir / f"{stem}.pdf", 1
    while candidate.exists() or candidate.is_symlink():
        candidate, n = ws.results_dir / f"{stem}_{n}.pdf", n + 1
    return candidate


def make_pdf(ws: Workspace, user: Optional[str], template_id: str, parameters: dict, signature: str = "blank",
             file_name=None, *, keep: bool = True) -> tuple[Optional[Path], str]:
    """Compile a template into /praca/wyniki (the v2 code, in a work folder of the conversation). Returns the PDF and
    the v2 result text, or (None, the v2 error text). With ``keep`` the values are saved under the PDF's name for
    "edit again"."""
    from src import agent_tools, run_context

    values = {str(k): ("" if v is None else str(v)) for k, v in (parameters or {}).items()}
    signature = signature if signature in ("blank", "scan") else "blank"
    work = ws.dir / "pdf"
    with run_context.use_context(user=user, workspace_root=str(work), session_id="robocze"):
        folder = run_context.workspace_dir()
        before = set(folder.glob("*.pdf"))
        result = agent_tools.tool_generate_pdf_from_template(template_id, values, signature=signature)
        made = sorted(set(folder.glob("*.pdf")) - before, key=lambda p: p.stat().st_mtime)
    if not result.startswith("SUKCES") or not made:
        return None, result
    target = result_name(ws, file_name, template_id)
    ws.results_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(made[-1]), target)
    if keep:
        _write(ws, target.name, {"template_id": template_id, "parameters": {k: v for k, v in values.items()
                                                                            if k != "_signature"},
                                 "file_name": target.name, "signature": signature})
    return target, f"SUKCES: zapisano /praca/wyniki/{target.name} ({target.stat().st_size} bajtów)"
