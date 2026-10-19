"""Uploaded files — summarise them, edit them, or add them to the company knowledge base.

The files are the attachments of the conversation (src/attachments.py copies them into the session
workspace). A step works on the attachments its input names, otherwise on those of the latest message
that had any.

- file_summary: the whole text; long files are summarised part by part, then the notes are combined.
- file_edit: .txt/.md/.docx keep their format; .pdf/.eml become .docx. Word documents keep their page
  set-up, paragraph styles, headings, lists and tables; character formatting inside a paragraph (bold,
  italics) is not carried over to the changed text.
- file_to_knowledge: copies the file to HAPILOT_ADDED_DOCS_DIR and indexes it at once (src/ingest_data.py),
  after the user confirms (approval policy ``ask``). Evaluation runs never change the knowledge base.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

DEFAULTS = {"single_pass_chars": 12000, "part_chars": 8000, "max_parts": 30}
SAME_FORMAT = {".txt", ".md", ".docx"}
TO_DOCX = {".pdf", ".eml"}
NO_FILE = ("Nie widzę w tej rozmowie załączonego pliku. Dołącz plik (ikona spinacza) i napisz, co mam z nim zrobić.")


# --------------------------------------------------------------------------- shared helpers

def _config() -> dict:
    try:
        from src.core.extension_manager import get_active

        return {**DEFAULTS, **(get_active().get_config("file_tools") or {})}
    except Exception:
        return dict(DEFAULTS)


def _step(state) -> dict:
    plan, idx = state.get("task_plan") or {}, state.get("current_step")
    steps = plan.get("steps", []) if isinstance(plan, dict) else []
    return steps[idx] if idx is not None and 0 <= idx < len(steps) else {}


def _request(state) -> str:
    from src.agent_graph import _resolve_effective_user_message
    from src.attachments import LISTING_PREFIX

    user = _resolve_effective_user_message(state["messages"])
    step = _step(state)
    parts = [step.get("action") or "", step.get("input") or ""]
    text = " — ".join(p for p in parts if p)
    # the planner's wording may drop details; the user's own words (without the attachment block) come along
    own = user.split(LISTING_PREFIX, 1)[0].strip()
    return f"{text}\n(Polecenie użytkownika: {own})" if text and own and own not in text else (text or own)


def _files(state, request: str) -> list[Path]:
    from src import attachments, run_context

    found = []
    for name in attachments.pick(request + " " + str(_step(state).get("input") or ""), state["messages"]):
        try:
            path = run_context.workspace_file(name)
        except ValueError:
            continue
        if path.is_file():
            found.append(path)
    return found


def _ask(llm, system_prompt: str, prompt: str) -> str:
    from src.core.tools_common import invoke_llm_and_normalize

    content = getattr(invoke_llm_and_normalize(llm, [SystemMessage(content=system_prompt), HumanMessage(content=prompt)]),
                      "content", "") or ""
    if content.startswith("[LLM"):
        raise RuntimeError(content[:300])
    return content.strip()


def _text(path: Path) -> str:
    from src.extractors import as_text, extract

    return as_text(extract(path))


def _done(state, message, **extra) -> dict:
    from src.agent_graph import _advance_plan_step

    return {"messages": [message], **extra, **_advance_plan_step(state)}


# --------------------------------------------------------------------------- summary

def summarize_text(text: str, name: str, request: str, llm, system_prompt: str, cfg: Optional[dict] = None) -> str:
    """Summary of a whole text; long texts part by part (at most ``max_parts`` parts, said so if cut)."""
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    cfg = {**DEFAULTS, **(cfg or {})}
    if len(text) <= cfg["single_pass_chars"]:
        return _ask(llm, system_prompt, f"PLIK: {name}\n<<<\n{text}\n>>>\n\nPOLECENIE: {request}\n"
                                        "Przygotuj odpowiedź na podstawie całego pliku.")
    splitter = RecursiveCharacterTextSplitter(chunk_size=cfg["part_chars"], chunk_overlap=0,
                                              separators=["\n\n", "\n", ". ", " ", ""])
    parts = splitter.split_text(text)
    used = parts[:cfg["max_parts"]]
    notes = []
    for i, part in enumerate(used, 1):
        notes.append(_ask(llm, system_prompt, (
            f"Fragment {i} z {len(parts)} pliku {name}:\n<<<\n{part}\n>>>\n\n"
            f"Polecenie użytkownika dotyczące całego pliku: {request}\n"
            "Wypisz zwięźle najważniejsze informacje z tego fragmentu (fakty, liczby, daty, decyzje, osoby).")))
    joined = "\n\n".join(f"[Fragment {i}]\n{n}" for i, n in enumerate(notes, 1))
    summary = _ask(llm, system_prompt, (
        f"Notatki z kolejnych fragmentów pliku {name}:\n{joined}\n\nPOLECENIE: {request}\n"
        "Na podstawie notatek przygotuj jedną spójną odpowiedź dotyczącą całego pliku."))
    if len(used) < len(parts):
        summary = (f"Uwaga: plik jest bardzo długi — streszczenie obejmuje pierwsze {len(used)} z {len(parts)} części "
                   f"(około {sum(map(len, used)):,} z {len(text):,} znaków).\n\n".replace(",", " ") + summary)
    return summary


def run_summarize_file(state):
    from src.llm_config import get_llm

    print("   --> 📑 [NODE: SUMMARIZE FILE]")
    request = _request(state)
    files = _files(state, request)
    if not files:
        return _done(state, AIMessage(content=NO_FILE), execution_status="error")
    llm, system_prompt = get_llm("file_assistant")
    sections = []
    for path in files:
        try:
            text = _text(path)
            if not text.strip():
                raise ValueError("plik nie zawiera tekstu")
            body = summarize_text(text, path.name, request, llm, system_prompt, _config())
        except Exception as exc:
            body = f"BŁĄD: nie udało się odczytać lub streścić pliku: {exc}"
        sections.append(f"**{path.name}**\n\n{body}" if len(files) > 1 else body)
    return _done(state, AIMessage(content="\n\n".join(sections)), execution_status="success")


# --------------------------------------------------------------------------- edit

def _style_name(paragraph) -> str:
    try:
        return (paragraph.style.name or "").lower()
    except Exception:
        return ""


def docx_to_markup(path: Path) -> str:
    """Word document as lines: '# ' headings, '- ' list items, '| a | b |' table rows, one paragraph per line."""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(path))
    lines: list[str] = []
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = Paragraph(child, doc)
            text = " ".join(para.text.split())
            if not text:
                continue
            style = _style_name(para)
            level = re.search(r"(?:heading|nagłówek)\s*(\d)", style)
            if style in ("title", "tytuł"):
                lines.append(f"# {text}")
            elif level:
                lines.append(f"{'#' * int(level.group(1))} {text}")
            elif "list" in style or "lista" in style:
                lines.append(f"- {text}")
            else:
                lines.append(text)
        elif tag == "tbl":
            if lines and not lines[-1].startswith("|"):
                lines.append("")
            for row in Table(child, doc).rows:
                cells = [" ".join(c.text.split()) for c in row.cells]
                lines.append("| " + " | ".join(cells) + " |")
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def _apply_style(doc, paragraph, *names: str) -> None:
    for name in names:
        try:
            paragraph.style = doc.styles[name]
            return
        except KeyError:
            continue


def markup_to_docx(markup: str, out: Path, template: Optional[Path] = None) -> None:
    """Write ``markup`` (see docx_to_markup) as a Word document; with ``template``, its page set-up and styles are kept."""
    from docx import Document

    doc = Document(str(template)) if template else Document()
    body = doc.element.body
    for child in list(body.iterchildren()):
        if child.tag.rsplit("}", 1)[-1] != "sectPr":
            body.remove(child)

    rows: list[list[str]] = []

    def flush_table() -> None:
        if not rows:
            return
        width = max(len(r) for r in rows)
        table = doc.add_table(rows=len(rows), cols=width)
        _apply_style(doc, table, "Table Grid")
        for r, cells in enumerate(rows):
            for c, value in enumerate(cells):
                table.cell(r, c).text = value
        rows.clear()

    for raw in markup.splitlines():
        line = raw.strip()
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):  # skip Markdown separator rows
                rows.append(cells)
            continue
        flush_table()
        if not line:
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        bullet = re.match(r"^[-*•]\s+(.*)$", line)
        if heading:
            para = doc.add_paragraph(heading.group(2))
            _apply_style(doc, para, f"Heading {len(heading.group(1))}")
        elif bullet:
            para = doc.add_paragraph(bullet.group(1))
            _apply_style(doc, para, "List Bullet")
        else:
            doc.add_paragraph(line)
    flush_table()
    doc.save(str(out))


def _strip_fences(text: str) -> str:
    text = text.strip()
    fenced = re.match(r"^```[a-zA-Z]*\n([\s\S]*?)\n```$", text)
    text = fenced.group(1) if fenced else text
    return text.strip().removeprefix("<<<").removesuffix(">>>").strip() + "\n"


def _output_path(source: Path, suffix: str) -> Path:
    base = source.with_name(f"{source.stem}_zmieniony{suffix}")
    n, target = 1, base
    while target.exists():
        target = base.with_name(f"{base.stem}_{n}{suffix}")
        n += 1
    return target


def edit_file(path: Path, request: str, llm, system_prompt: str, max_chars: int) -> Path:
    """Apply the requested change; returns the new file in the same folder. Raises ValueError for unsupported input."""
    suffix = path.suffix.lower()
    if suffix not in SAME_FORMAT | TO_DOCX:
        raise ValueError(f"edycja plików {suffix} nie jest obsługiwana (obsługiwane: .docx, .txt, .md, .pdf, .eml)"
                         + ("; dla arkusza poproś o obliczenia albo streszczenie" if suffix == ".xlsx" else ""))
    structured = suffix == ".docx"
    original = docx_to_markup(path) if structured else _text(path)
    if len(original) > max_chars:
        raise ValueError(f"plik ma {len(original)} znaków, więcej niż limit edycji ({max_chars}); "
                         "podziel go albo poproś o zmianę konkretnego fragmentu w krótszym pliku")
    fmt = ("Format: nagłówki zaczynają się od #, punkty listy od „- ”, wiersze tabel od „|”; jeden akapit w jednej linii.\n"
           if structured else "")
    edited = _strip_fences(_ask(llm, system_prompt, (
        f"PLIK: {path.name}\n{fmt}TREŚĆ:\n<<<\n{original}\n>>>\n\nPOLECENIE: {request}\n\n"
        "Zwróć CAŁY zmieniony dokument w tym samym formacie — bez komentarzy, bez wyjaśnień i bez bloków kodu.")))
    if len(edited.strip()) < 2:
        raise RuntimeError("model zwrócił pustą treść")
    if suffix in (".txt", ".md"):
        out = _output_path(path, suffix)
        out.write_text(edited, encoding="utf-8")
    else:
        out = _output_path(path, ".docx")
        markup_to_docx(edited, out, template=path if structured else None)
    return out


def run_edit_file(state):
    from src.config import get_settings
    from src.llm_config import get_llm

    print("   --> 🖊️ [NODE: EDIT FILE]")
    request = _request(state)
    files = _files(state, request)
    if not files:
        return _done(state, AIMessage(content=NO_FILE), execution_status="error")
    if len(files) > 1:
        names = ", ".join(p.name for p in files)
        return _done(state, AIMessage(content=f"Załączonych jest kilka plików ({names}). Napisz, który mam zmienić."),
                     execution_status="error")
    path = files[0]
    llm, system_prompt = get_llm("file_assistant")
    try:
        out = edit_file(path, request, llm, system_prompt, get_settings().max_edit_chars)
    except Exception as exc:
        return {"messages": [HumanMessage(content=f"WYNIK:\nBŁĄD: Nie zmieniłem pliku {path.name}: {exc}")],
                "execution_status": "error", "current_step": None}
    note = " (z pliku PDF/e-mail powstał dokument Word)" if path.suffix.lower() in TO_DOCX else ""
    return _done(state, HumanMessage(content=(f"WYNIK:\nSUKCES! Wygenerowano plik: {out.name} ({out.stat().st_size} bajtów)\n"
                                              f"Nowa wersja pliku {path.name}{note}.")), execution_status="success")


# --------------------------------------------------------------------------- knowledge base

def _add(state, names: list[str]) -> dict:
    from src import run_context
    from src.config import get_settings
    from src.ingest_data import add_documents

    settings = get_settings()
    paths = []
    for name in names:
        try:
            path = run_context.workspace_file(name)
        except ValueError:
            continue
        if path.is_file():
            paths.append(path)
    if not paths:
        return _done(state, AIMessage(content=NO_FILE), hitl_response=None, execution_status="error")
    try:
        result = add_documents(paths, docs_dir=settings.docs_dir, added_dir=settings.added_docs_dir,
                               collection=settings.collection, path=settings.chroma_path)
    except Exception as exc:
        return _done(state, AIMessage(content=f"BŁĄD: Nie udało się dodać plików do bazy wiedzy: {exc}"), hitl_response=None,
                     execution_status="error")
    lines = []
    if result["added"]:
        lines.append(f"📚 Dodano do bazy wiedzy: {', '.join(result['added'])} ({result['chunks']} fragm.). "
                     "Od teraz odpowiadam także na podstawie tego dokumentu, we wszystkich rozmowach.")
    if result["skipped"]:
        lines.append("Nie dodano (nie udało się odczytać tekstu): " + ", ".join(Path(s["file"]).name for s in result["skipped"]))
    return _done(state, AIMessage(content="\n".join(lines)), hitl_response=None,
                 execution_status="success" if result["added"] else "error")


def run_add_to_knowledge(state):
    from src import run_context, tracing
    from src.core.tools_common import create_hitl_confirm_request

    print("   --> 📚 [NODE: ADD TO KNOWLEDGE]")
    response = state.get("hitl_response") or {}
    if response.get("resume_node") == "add_to_knowledge":
        if not response.get("confirmed"):
            return _done(state, AIMessage(content="Rozumiem — nie dodałem plików do bazy wiedzy."), hitl_response=None)
        return _add(state, list(response.get("files") or []))

    files = _files(state, _request(state))
    if not files:
        return _done(state, AIMessage(content=NO_FILE), execution_status="error")
    names = [p.name for p in files]
    policy = run_context.approval_policy()
    if run_context.current("kb_path"):  # evaluation run: its corpus is fixed
        tracing.event("approval", kind="add_to_knowledge", policy=policy, decision="not_added",
                      summary="evaluation runs do not change the knowledge base")
        return _done(state, AIMessage(content=(
            f"W tym przebiegu baza wiedzy jest stała, więc nie dodałem: {', '.join(names)}. "
            "Treść pliku jest dostępna w tej rozmowie.")))
    if policy == "auto":
        tracing.event("approval", kind="add_to_knowledge", policy="auto", decision="auto_approved", summary=", ".join(names))
        return _add(state, names)
    listing = "\n".join(f"- {n}" for n in names)
    hitl = create_hitl_confirm_request(
        content=(f"📚 **Dodać do bazy wiedzy firmy?**\n\n{listing}\n\n"
                 "Plik zostanie zapisany w bazie wiedzy tej instalacji Hapilota i będzie przeszukiwany we wszystkich rozmowach."),
        continuation={"type": "resume_node", "resume_node": "add_to_knowledge", "files": names,
                      "saved_plan": state.get("task_plan") or {}, "saved_step": state.get("current_step")},
    )
    return {"messages": [AIMessage(content="Oczekuję na potwierdzenie dodania pliku do bazy wiedzy.")], "hitl_request": hitl}
