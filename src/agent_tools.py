"""Tools used by the agent's nodes: planning, knowledge search, LaTeX documents, Python execution.

No side effects at import time: Docker, the knowledge base and the language models are reached
only when a tool runs. Files are read and written in the current session's workspace
(``src.run_context.workspace_dir``); code and LaTeX run in the isolated sandbox (``src.sandbox``).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage

from src import knowledge_base, run_context, sandbox
from src.tracing import traced_tool
from src.config import PROJECT_ROOT, get_settings
from src.core.tools_common import (  # re-exported for extensions
    TaskState,
    create_hitl_action_request,
    create_hitl_confirm_request,
    create_hitl_form_request,
    invoke_llm_and_normalize,
)
from src.llm_config import get_llm

__all__ = [
    "TaskState", "create_hitl_action_request", "create_hitl_confirm_request", "create_hitl_form_request",
    "invoke_llm_and_normalize", "tool_rag_search", "rag_search", "tool_plan_task", "tool_list_templates",
    "tool_generate_pdf_from_template", "tool_generate_pdf", "tool_execute_python", "tool_read_sandbox_file",
    "tool_modify_document", "build_document_hitl_form", "normalize_template_params",
]

TEMPLATES_DIR = PROJECT_ROOT / "templates"
TEMPLATES_METADATA_FILE = TEMPLATES_DIR / "templates.json"


# =========================================================================== knowledge base

@traced_tool("rag_search")
def rag_search(query: str, k: Optional[int] = None) -> list[knowledge_base.Chunk]:
    """Structured search in the company knowledge base."""
    print(f"🔎 [TOOL USE] RAG Search: '{query[:120]}'")
    return knowledge_base.search(query, k=k)


def tool_rag_search(query: str) -> str:
    """Knowledge-base search formatted for a prompt (``--- ŹRÓDŁO: file ---`` blocks)."""
    try:
        return knowledge_base.format_chunks(rag_search(query))
    except Exception as exc:
        return f"Błąd RAG: {exc}"


# =========================================================================== planning

# Planner-facing descriptions of the tools; only tools registered by enabled extensions are offered.
PLANNER_TOOL_DESCRIPTIONS = {
    "rag_search": "Wyszukiwanie w dokumentach firmowych (RAG)",
    "llm_synthesis": "Generowanie/synteza tekstu przez model językowy",
    "template_pdf": "Generowanie PDF z szablonu LaTeX (list, raport, protokół). Parametry dokumentu są pokazywane użytkownikowi do potwierdzenia.",
    "modify_document": "Modyfikacja wcześniej wygenerowanego dokumentu (zmiana daty, adresu, treści) i ponowne wygenerowanie PDF",
    "python": "Wykonanie kodu Python w izolowanym sandboxie (obliczenia, analiza danych, wykresy); kod może czytać dokumenty firmy (np. arkusze .xlsx) i załączniki",
    "calendar": "Sprawdzenie kalendarza (integracja n8n)",
    "email": "Przygotowanie wersji roboczej e-maila (nic nie jest wysyłane automatycznie)",
    "chat": "Zwykła rozmowa z użytkownikiem (luźne pytania, powitania, zgoda/odmowa)",
    "hitl_confirm": "Zatrzymaj się i poproś użytkownika o potwierdzenie/decyzję. NIE dodawaj przed template_pdf — to jest automatyczne.",
}

ALWAYS_AVAILABLE_TOOLS = ("llm_synthesis", "chat", "hitl_confirm")


def available_planner_tools() -> list[str]:
    """Tool names the planner may use: the core ones plus those registered by enabled extensions."""
    names = list(ALWAYS_AVAILABLE_TOOLS)
    try:
        from src.core.extension_manager import extension_manager

        registered = set(extension_manager.get_planner_tool_names())
    except Exception:
        registered = set(PLANNER_TOOL_DESCRIPTIONS)
    for name in PLANNER_TOOL_DESCRIPTIONS:
        if name in registered and name not in names:
            names.append(name)
    return names


def tool_plan_task(query: str, plan_mode: str = "auto", history_context: str = "") -> str:
    """Ask the planner model for a JSON plan (task name, complexity, steps with tools)."""
    print(f"📋 [TOOL USE] Task Planning (mode={plan_mode}): '{query[:50]}...'")
    llm, system_prompt = get_llm("task_planner")
    tools = available_planner_tools()
    tool_lines = "\n".join(f"- {name}: {PLANNER_TOOL_DESCRIPTIONS[name]}" for name in tools)
    history = f"HISTORIA OSTATNICH AKCJI I DOKUMENTÓW (jako kontekst, nie odpowiadaj na to wprost):\n{history_context}\n" if history_context else ""

    prompt = f"""Przeanalizuj to zapytanie i wygeneruj strukturalny plan wykonania.
Rozbijaj zadania na małe, niezależne kroki.

{history}ZAPYTANIE: "{query}"

GENERUJ ODPOWIEDŹ W JSON Z PONIŻSZĄ STRUKTURĄ:
{{
  "task_name": "krótka nazwa zadania",
  "complexity": "low|medium|high",
  "steps": [
    {{"id": 1, "action": "opis akcji", "tool": "{'|'.join(tools)}", "input": "konkretny input"}}
  ]
}}

DOSTĘPNE NARZĘDZIA (używaj wyłącznie tych):
{tool_lines}

PRZYKŁADY:
1. "Co robić w razie awarii serwera i napisz o tym raport PDF"
   - Krok 1: rag_search (szukanie procedury), Krok 2: llm_synthesis (synteza), Krok 3: template_pdf (raport)
2. "Napisz podanie o urlop" — Krok 1: template_pdf
3. "Jak tam dzień minął?" — Krok 1: chat
4. "Zmień datę w tym piśmie na 7 marca 2026" — Krok 1: modify_document
5. "Policz średnią z liczb 3, 5 i 10" — Krok 1: python (w polu input opisz zadanie obliczeniowe)

INSTRUKCJE:
- Gdy użytkownik prosi o zmianę ISTNIEJĄCEGO dokumentu (zmień, popraw, zaktualizuj w wygenerowanym piśmie) -> modify_document
- Kontynuacja rozmowy ("krócej", "napisz to prościej", "nie, dzięki", "jak się masz") -> bez rag_search; użyj chat lub llm_synthesis
- Jeśli trzeba NAJPIERW znaleźć informacje, POTEM coś napisać -> co najmniej 2 kroki
- Nowe dokumenty PDF tylko narzędziem template_pdf, i tylko gdy użytkownik o dokument prosi
- Brak wyników RAG nie blokuje planu generowania PDF
- Odpowiedz wyłącznie obiektem JSON
"""

    try:
        response = invoke_llm_and_normalize(llm, [SystemMessage(content=system_prompt), HumanMessage(content=prompt)])
        content = getattr(response, "content", "") or ""
        if content.startswith("[LLM"):
            raise RuntimeError(content)
        match = re.search(r"\{.*\}", content, re.DOTALL)
        return match.group(0) if match else content
    except Exception as exc:
        # No plan → the router sends the request to the chat node, which reports the problem.
        return json.dumps({"error": str(exc), "task_name": query[:30], "complexity": "low", "steps": []})


# =========================================================================== LaTeX templates

def _load_templates_metadata() -> dict:
    try:
        return json.loads(TEMPLATES_METADATA_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": str(exc), "templates": {}}


def _sanitize_latex_value(value: str) -> str:
    """Escape LaTeX special characters in a parameter value while preserving
    intentional LaTeX commands (\\begin, \\end, \\item, \\textbf, \\\\, etc.).

    Strategy:
    1. Temporarily replace known LaTeX commands with placeholders
    2. Escape bare special characters: _ & % # $
    3. Restore the LaTeX commands from placeholders
    """
    if not value:
        return value

    # Known LaTeX command patterns to preserve (order matters)
    PRESERVED_PATTERNS = [
        (r'\\begin\{[^}]+\}', '__LATEX_BEGIN_{}__'),
        (r'\\end\{[^}]+\}', '__LATEX_END_{}__'),
        (r'\\item\b', '__LATEX_ITEM__'),
        (r'\\\\', '__LATEX_DBLBS__'),
        (r'\\textbf\{[^}]*\}', '__LATEX_TEXTBF_{}__'),
        (r'\\textit\{[^}]*\}', '__LATEX_TEXTIT_{}__'),
        (r'\\emph\{[^}]*\}', '__LATEX_EMPH_{}__'),
        (r'\\underline\{[^}]*\}', '__LATEX_UNDERLINE_{}__'),
        (r'\\hfill\b', '__LATEX_HFILL__'),
        (r'\\vskip\s+[\d.]+\s*\w+', '__LATEX_VSKIP_{}__'),
        (r'\\noindent\b', '__LATEX_NOINDENT__'),
        (r'\\newline\b', '__LATEX_NEWLINE__'),
        (r'\\par\b', '__LATEX_PAR__'),
        (r'\\Large\b', '__LATEX_LARGE__'),
        (r'\\large\b', '__LATEX_LOWERL__'),
        (r'\\small\b', '__LATEX_SMALL__'),
        (r'\\footnotesize\b', '__LATEX_FNSIZE__'),
        (r'\\phantom\{[^}]*\}', '__LATEX_PHANTOM_{}__'),
        (r'\\hspace\{[^}]*\}', '__LATEX_HSPACE_{}__'),
        (r'\\vspace\{[^}]*\}', '__LATEX_VSPACE_{}__'),
        (r'\\section\{[^}]*\}', '__LATEX_SECTION_{}__'),
        (r'\\subsection\{[^}]*\}', '__LATEX_SUBSECTION_{}__'),
    ]

    # Step 1: Protect known LaTeX commands
    preserved = []
    result = value
    for pattern, placeholder_tmpl in PRESERVED_PATTERNS:
        matches = list(re.finditer(pattern, result))
        for i, m in enumerate(reversed(matches)):
            idx = len(preserved)
            placeholder = placeholder_tmpl.replace('{}', str(idx))
            preserved.append((placeholder, m.group(0)))
            result = result[:m.start()] + placeholder + result[m.end():]

    # Step 2: Escape special characters that break LaTeX
    # Order matters: backslash-dependent ones first
    # Don't escape { } as they may be part of preserved patterns or intentional grouping
    result = result.replace('&', r'\&')
    result = result.replace('%', r'\%')
    result = result.replace('#', r'\#')
    result = result.replace('$', r'\$')
    # Escape underscores only if not already escaped
    result = re.sub(r'(?<!\\)_', r'\\_', result)
    # Escape ~ and ^ only as bare characters (not in commands)
    result = re.sub(r'(?<!\\)~', r'\textasciitilde{}', result)
    result = re.sub(r'(?<!\\)\^', r'\textasciicircum{}', result)

    # Step 3: Restore preserved LaTeX commands
    for placeholder, original in reversed(preserved):
        result = result.replace(placeholder, original)

    return result


def _fill_template(template_content: str, parameters: dict) -> str:
    """Replace ``{{PARAM}}`` placeholders with sanitised values."""
    result = template_content
    for key, value in parameters.items():
        result = result.replace("{{" + key + "}}", _sanitize_latex_value(str(value)))
    return result


def _get_condensed_templates_info() -> str:
    """Short template list for prompts (required parameters marked with *)."""
    lines = []
    for tid, info in _load_templates_metadata().get("templates", {}).items():
        params = ", ".join(f"{p}{' *' if d.get('required', True) else ''}" for p, d in info.get("parameters", {}).items())
        lines.append(f"- {tid}: {info.get('name', tid)} — {info.get('description', '')[:80]}")
        lines.append(f"  Parametry: {params}")
    return "\n".join(lines)


def tool_list_templates() -> str:
    """Available LaTeX templates with descriptions and parameters (JSON)."""
    metadata = _load_templates_metadata()
    if "error" in metadata and not metadata.get("templates"):
        return f"BŁĄD: Nie można wczytać szablonów: {metadata['error']}"
    result = []
    for template_id, info in metadata.get("templates", {}).items():
        params = {name: {"opis": p["description"], "przykład": p.get("example", ""), "wymagany": p.get("required", True)}
                  for name, p in info.get("parameters", {}).items()}
        result.append({"id": template_id, "nazwa": info["name"], "opis": info["description"], "parametry": params})
    return json.dumps(result, ensure_ascii=False, indent=2)


def _generate_unique_filename(prefix: str = "dokument") -> str:
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.tex"


def _params_path(tex_name: str) -> Path:
    return run_context.workspace_file(Path(tex_name).stem + ".params.json")


@traced_tool("generate_pdf_from_template")
def tool_generate_pdf_from_template(template_id: str, parameters: dict) -> str:
    """Fill a template, store its parameters next to the .tex, and compile it to PDF."""
    print(f"📄 [TOOL USE] Generate PDF from template: {template_id}")
    templates = _load_templates_metadata().get("templates", {})
    if template_id not in templates:
        return f"BŁĄD: Nieznany szablon '{template_id}'. Dostępne szablony: {', '.join(templates)}"
    info = templates[template_id]
    template_file = TEMPLATES_DIR / info["file"]
    if not template_file.is_file():
        return f"BŁĄD: Plik szablonu nie istnieje: {info['file']}"

    parameters = normalize_template_params(template_id, parameters or {})
    missing = [f"{name} ({p['description']})" for name, p in info.get("parameters", {}).items()
               if p.get("required", True) and not str(parameters.get(name, "")).strip()]
    if missing:
        return "BŁĄD: Brakujące wymagane parametry:\n" + "\n".join(f"  - {m}" for m in missing)

    filled = _fill_template(template_file.read_text(encoding="utf-8"), parameters)
    for placeholder in re.findall(r"\{\{([A-Z_]+)\}\}", filled):  # unfilled optional parameters
        filled = filled.replace("{{" + placeholder + "}}", "")

    filename = _generate_unique_filename(prefix=template_id)
    _params_path(filename).write_text(json.dumps({"template_id": template_id, "parameters": parameters},
                                                 ensure_ascii=False, indent=2), encoding="utf-8")
    return tool_generate_pdf(filled, filename)


# =========================================================================== sandbox: LaTeX and Python

@traced_tool("compile_latex")
def tool_generate_pdf(latex_content: str, filename: Optional[str] = None) -> str:
    """Compile LaTeX to PDF in the sandbox; the files stay in the session workspace."""
    filename = filename or _generate_unique_filename()
    clean_latex = _extract_latex_from_llm_output(latex_content)
    if "\\documentclass" not in clean_latex:
        return "BŁĄD: Wyciągnięty tekst nie zawiera \\documentclass. Nie jest to poprawny kod LaTeX."

    tex_path = run_context.workspace_file(filename)
    base = tex_path.stem
    pdf_path = tex_path.with_suffix(".pdf")
    tex_path.write_text(clean_latex, encoding="utf-8")
    print(f"📄 [TOOL USE] Generowanie PDF: {filename}")

    compile_cmd = ["sh", "-c", f"pdflatex -interaction=nonstopmode -halt-on-error {tex_path.name} >/dev/null"
                   f" && pdflatex -interaction=nonstopmode {tex_path.name} >/dev/null"]
    try:
        result = sandbox.run(compile_cmd, tex_path.parent, timeout=get_settings().latex_timeout)
    except sandbox.SandboxUnavailable as exc:
        return f"BŁĄD: {exc}"

    if pdf_path.is_file() and result.ok:
        for ext in ("aux", "log", "out", "toc", "nav", "snm", "vrb", "fls", "fdb_latexmk", "synctex.gz", "bbl", "blg"):
            (tex_path.parent / f"{base}.{ext}").unlink(missing_ok=True)
        return f"SUKCES! Wygenerowano plik: {pdf_path.name} ({pdf_path.stat().st_size} bajtów)"

    log_file = tex_path.parent / f"{base}.log"
    lines = log_file.read_text(encoding="utf-8", errors="ignore").splitlines() if log_file.is_file() else []
    errors: list[str] = []
    for i, line in enumerate(lines):
        if line.startswith("!") or "Error" in line or "Fatal" in line:
            errors.extend(lines[max(0, i - 2): i + 3] + ["---"])
    detail = "\n".join(errors[-40:] or lines[-30:] or [result.output[-2000:]])
    if result.timed_out:
        return f"BŁĄD KOMPILACJI: przekroczono limit czasu ({get_settings().latex_timeout} s).\n{detail}"
    return f"BŁĄD KOMPILACJI (exit_code={result.exit_code}):\n{detail}"


def data_dir() -> Optional[Path]:
    """Company documents readable by sandboxed code (read-only at /data); the evaluation corpus in eval runs."""
    path = Path(run_context.current("docs_dir") or get_settings().docs_dir)
    return path if path.is_dir() else None


def files_for_code(limit: int = 200) -> str:
    """Listing of files code can read: company documents under /data and files in the session workspace."""
    lines = []
    docs = data_dir()
    if docs is not None:
        names = sorted(p.relative_to(docs).as_posix() for p in docs.rglob("*")
                       if p.is_file() and not any(part.startswith(".") for part in p.relative_to(docs).parts))
        shown = names[:limit] + ([f"… i {len(names) - limit} innych"] if len(names) > limit else [])
        lines.append("Dokumenty firmy (tylko do odczytu) w katalogu /data: " + ", ".join(f"/data/{n}" for n in shown))
    workspace = [p.name for p in run_context.workspace_dir().iterdir() if p.is_file() and not p.name.startswith("snippet_")]
    if workspace:
        lines.append("Pliki sesji (załączniki) w bieżącym katalogu: " + ", ".join(sorted(workspace)))
    return "\n".join(lines)


@traced_tool("execute_python")
def tool_execute_python(code: str) -> str:
    """Run Python code in the sandbox. ``code`` may be raw code or an LLM answer with a code block."""
    clean_code = _extract_python_from_llm_output(code)
    if not clean_code.strip():
        return "BŁĄD: Nie udało się wyekstrahować kodu Python z odpowiedzi."
    script = run_context.workspace_file(f"snippet_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.py")
    script.write_text(clean_code, encoding="utf-8")
    print(f"💻 [TOOL USE] Executing Python in sandbox ({script.name})")
    try:
        result = sandbox.run(["python3", script.name], script.parent, timeout=get_settings().python_timeout, data_dir=data_dir())
    except sandbox.SandboxUnavailable as exc:
        return f"BŁĄD: {exc}"
    if result.timed_out:
        return f"BŁĄD WYKONANIA:\nPrzekroczono limit czasu ({get_settings().python_timeout} s).\n{result.output[-2000:]}"
    if result.exit_code == 0:
        return f"SUKCES:\n{result.output or '[Brak outputu]'}"
    return f"BŁĄD WYKONANIA:\n{result.output}"


def _extract_python_from_llm_output(raw_content: str) -> str:
    """Robustne wyciąganie kodu Python z odpowiedzi LLM. Uodpornione na gadatliwość bota."""
    if not raw_content or not raw_content.strip():
        return ""
    
    # 1. Explicit ```python block (wybierz najdłuższy, bo LLM może dać kilka)
    python_blocks = re.findall(r'```(?:python|py)\s*\n(.*?)```', raw_content, re.DOTALL)
    if python_blocks:
        return max(python_blocks, key=len).strip()
    
    # 2. Any ``` block
    code_blocks = re.findall(r'```(?:.*?)\s*\n(.*?)```', raw_content, re.DOTALL)
    if code_blocks:
        return max(code_blocks, key=len).strip()
        
    # 3. Jeśli nie ma bloków markdown, przeanalizuj surowy tekst
    stripped = raw_content.strip()
    
    # Heurystyka do odrzucania polsko/angielskojęzycznego lania wody
    common_words = ['oto', 'poniżej', 'wynik', 'kod', 'to', 'jest', 'proszę', 'here', 'this', 'below', 'sure', 'oczywiście', 'pewnie', 'kod:']
    first_word = stripped.split()[0].lower().rstrip(':,.') if stripped.split() else ""
    if first_word in common_words:
        print(f"      ⚠️ [EXTRACT] Odrzucono próbę wstrzyknięcia języka naturalnego: '{stripped[:50]}...'")
        return ""
        
    # Check for python indicators
    has_python_keywords = any(kw in stripped for kw in [
        'import ', 'from ', 'def ', 'class ', 'print(', 'return ',
        'for ', 'while ', 'if __name__'
    ])
    
    if has_python_keywords:
        return stripped
        
    return ""


def _extract_latex_from_llm_output(raw_content: str) -> str:
    """Robustne wyciąganie kodu LaTeX z odpowiedzi LLM.
    
    Strategia:
    1. Szukaj bloku ```latex ... ``` (preferowany)
    2. Szukaj bloku ```tex ... ```
    3. Szukaj \\documentclass ... \\end{document} bezpośrednio
    4. Szukaj dowolnego bloku ``` ... ``` zawierającego \\documentclass
    5. Fallback: zwróć surowy input
    """
    # 1. Explicit ```latex block
    match = re.search(r'```latex\s*\n(.*?)```', raw_content, re.DOTALL)
    if match:
        return match.group(1).strip()
    
    # 2. ```tex block
    match = re.search(r'```tex\s*\n(.*?)```', raw_content, re.DOTALL)
    if match:
        return match.group(1).strip()
    
    # 3. Direct \documentclass ... \end{document}
    match = re.search(r'(\\documentclass.*?\\end\{document\})', raw_content, re.DOTALL)
    if match:
        return match.group(1).strip()
    
    # 4. Any ``` block containing \documentclass
    code_blocks = re.findall(r'```(?:\w*)?\s*\n(.*?)```', raw_content, re.DOTALL)
    for block in code_blocks:
        if '\\documentclass' in block:
            return block.strip()
    
    # 5. Fallback
    return raw_content.strip()


@traced_tool("read_file")
def tool_read_sandbox_file(filename: str) -> str:
    """Read a text file from the current session's workspace."""
    try:
        path = run_context.workspace_file(filename)
    except ValueError as exc:
        return f"BŁĄD: {exc}"
    if not path.is_file():
        recent = sorted(path.parent.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
        hint = f" Ostatnie pliki: {', '.join(p.name for p in recent)}" if recent else ""
        return f"BŁĄD: Plik '{filename}' nie istnieje w tej sesji.{hint}"
    try:
        return f"[Plik: {filename}, {path.stat().st_size} bajtów]\n{path.read_text(encoding='utf-8')}"
    except UnicodeDecodeError:
        return f"BŁĄD: Plik '{filename}' nie jest plikiem tekstowym."


# =========================================================================== modifying a document

def _latest_tex_in_session() -> Optional[Path]:
    files = sorted(run_context.workspace_dir().glob("*.tex"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


@traced_tool("modify_document")
def tool_modify_document(tex_filename: str, modifications_prompt: str, context: str = "") -> str:
    """Apply a requested change to a document generated in this session and recompile it.

    Documents made from a template are edited through their stored parameters (small, structured,
    never truncated). Other .tex files are edited whole, up to ``HAPILOT_MAX_EDIT_CHARS``.
    """
    print(f"📝 [TOOL USE] Modify document: {tex_filename or '(latest in session)'}")
    tex_path: Optional[Path] = None
    if tex_filename:
        try:
            candidate = run_context.workspace_file(tex_filename)
            tex_path = candidate if candidate.is_file() else None
        except ValueError:
            tex_path = None
    tex_path = tex_path or _latest_tex_in_session()
    if tex_path is None:
        return "BŁĄD: W tej sesji nie wygenerowano jeszcze żadnego dokumentu. Najpierw wygeneruj dokument."

    llm, system_prompt = get_llm("template_filler")
    params_file = tex_path.with_name(tex_path.stem + ".params.json")
    if params_file.is_file():
        saved = json.loads(params_file.read_text(encoding="utf-8"))
        prompt = f"""Oto parametry istniejącego dokumentu (szablon: {saved['template_id']}) w formacie JSON:
{json.dumps(saved['parameters'], ensure_ascii=False, indent=2)}

ŻĄDANE ZMIANY:
{modifications_prompt}
{f'{chr(10)}DODATKOWY KONTEKST: {context}' if context else ''}

Wprowadź WYŁĄCZNIE żądane zmiany. Pozostałe wartości przepisz bez zmian.
Odpowiedz WYŁĄCZNIE obiektem JSON z kompletem parametrów (te same klucze)."""
        response = invoke_llm_and_normalize(llm, [SystemMessage(content=system_prompt), HumanMessage(content=prompt)])
        content = getattr(response, "content", "") or ""
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if content.startswith("[LLM") or not match:
            return f"BŁĄD: Model nie zwrócił poprawnych parametrów dokumentu.\n{content[:300]}"
        try:
            updated = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            return f"BŁĄD: Nieprawidłowy JSON w odpowiedzi modelu: {exc}"
        if isinstance(updated.get("parameters"), dict):
            updated = updated["parameters"]
        merged = {**saved["parameters"], **normalize_template_params(saved["template_id"], updated)}
        return tool_generate_pdf_from_template(saved["template_id"], merged)

    original = tex_path.read_text(encoding="utf-8")
    limit = get_settings().max_edit_chars
    if len(original) > limit:
        return (f"BŁĄD: Dokument ma {len(original)} znaków, więcej niż limit edycji ({limit}). "
                "Nie skracam go, żeby nie utracić treści — wygeneruj dokument ponownie z szablonu.")
    prompt = f"""Masz poniższy dokument LaTeX. Wprowadź TYLKO żądane zmiany, zachowując resztę bez zmian.

ORYGINALNY DOKUMENT:
```latex
{original}
```

ŻĄDANE ZMIANY:
{modifications_prompt}
{f'{chr(10)}DODATKOWY KONTEKST: {context}' if context else ''}

Zwróć KOMPLETNY zmodyfikowany kod LaTeX (cały dokument), bez dodatkowego tekstu."""
    response = invoke_llm_and_normalize(llm, [SystemMessage(content=system_prompt), HumanMessage(content=prompt)])
    modified = _extract_latex_from_llm_output(getattr(response, "content", "") or "")
    if "\\documentclass" not in modified:
        return "BŁĄD: Model nie zwrócił poprawnego dokumentu LaTeX."
    prefix = tex_path.stem.split("_")[0]
    return tool_generate_pdf(modified, _generate_unique_filename(prefix=prefix))


# Backwards-compatible name
def tool_modify_and_recompile_tex(tex_filename: str, modifications_prompt: str, context: str = "") -> str:
    return tool_modify_document(tex_filename, modifications_prompt, context)


# =========================================================================== template parameters and HITL form

def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(key).lower())


def _param_alias_map(template_id: str, param_definitions: dict) -> dict:
    """Normalised key -> canonical template parameter (template keys plus common aliases)."""
    # Canonicalize incoming params to actual template keys to avoid missing required fields.
    alias_map = {}
    for canonical in param_definitions.keys():
        alias_map[_normalize_key(canonical)] = canonical

    # Common alias mappings for known templates (best-effort normalization).
    if template_id == "letter":
        alias_map.update({
            _normalize_key("sender"): "SENDER_NAME",
            _normalize_key("sender_name"): "SENDER_NAME",
            _normalize_key("nadawca"): "SENDER_NAME",
            _normalize_key("podpis"): "SENDER_NAME",
            _normalize_key("signature"): "SENDER_NAME",
            _normalize_key("sender_company"): "SENDER_COMPANY",
            _normalize_key("sender_address"): "SENDER_ADDRESS",
            _normalize_key("email"): "SENDER_EMAIL",
            _normalize_key("sender_email"): "SENDER_EMAIL",
            _normalize_key("recipient"): "RECIPIENT_NAME",
            _normalize_key("recipient_name"): "RECIPIENT_NAME",
            _normalize_key("adresat"): "RECIPIENT_NAME",
            _normalize_key("recipient_company"): "RECIPIENT_COMPANY",
            _normalize_key("recipient_address"): "RECIPIENT_ADDRESS",
            _normalize_key("recipient_title"): "RECIPIENT_TITLE",
            _normalize_key("date"): "CITY_DATE",
            _normalize_key("data"): "CITY_DATE",
            _normalize_key("city_date"): "CITY_DATE",
            _normalize_key("subject"): "DOCUMENT_TITLE",
            _normalize_key("title"): "DOCUMENT_TITLE",
            _normalize_key("tytul"): "DOCUMENT_TITLE",
            _normalize_key("greeting"): "GREETING",
            _normalize_key("powitanie"): "GREETING",
            _normalize_key("salutation"): "GREETING",
            _normalize_key("body"): "BODY",
            _normalize_key("tresc"): "BODY",
            _normalize_key("content"): "BODY",
            _normalize_key("closing"): "CLOSING",
            _normalize_key("pozdrowienia"): "CLOSING",
        })
    elif template_id == "report":
        alias_map.update({
            _normalize_key("institution"): "INSTITUTION",
            _normalize_key("instytucja"): "INSTITUTION",
            _normalize_key("title"): "REPORT_TITLE",
            _normalize_key("report_title"): "REPORT_TITLE",
            _normalize_key("tytul"): "REPORT_TITLE",
            _normalize_key("subtitle"): "REPORT_SUBTITLE",
            _normalize_key("report_subtitle"): "REPORT_SUBTITLE",
            _normalize_key("podtytul"): "REPORT_SUBTITLE",
            _normalize_key("author"): "AUTHOR_NAME",
            _normalize_key("author_name"): "AUTHOR_NAME",
            _normalize_key("autor"): "AUTHOR_NAME",
            _normalize_key("author_position"): "AUTHOR_POSITION",
            _normalize_key("stanowisko"): "AUTHOR_POSITION",
            _normalize_key("department"): "DEPARTMENT",
            _normalize_key("dzial"): "DEPARTMENT",
            _normalize_key("zaklad"): "DEPARTMENT",
            _normalize_key("report_date"): "REPORT_DATE",
            _normalize_key("date"): "REPORT_DATE",
            _normalize_key("data"): "REPORT_DATE",
            _normalize_key("report_number"): "REPORT_NUMBER",
            _normalize_key("numer"): "REPORT_NUMBER",
            _normalize_key("intro"): "SECTION_INTRODUCTION",
            _normalize_key("introduction"): "SECTION_INTRODUCTION",
            _normalize_key("wstep"): "SECTION_INTRODUCTION",
            _normalize_key("wprowadzenie"): "SECTION_INTRODUCTION",
            _normalize_key("scope"): "SECTION_SCOPE",
            _normalize_key("zakres"): "SECTION_SCOPE",
            _normalize_key("analysis"): "SECTION_ANALYSIS",
            _normalize_key("analiza"): "SECTION_ANALYSIS",
            _normalize_key("results"): "SECTION_RESULTS",
            _normalize_key("wyniki"): "SECTION_RESULTS",
            _normalize_key("conclusions"): "SECTION_CONCLUSIONS",
            _normalize_key("wnioski"): "SECTION_CONCLUSIONS",
            _normalize_key("rekomendacje"): "SECTION_CONCLUSIONS",
            _normalize_key("approver"): "APPROVER_NAME",
            _normalize_key("zatwierdzajacy"): "APPROVER_NAME",
        })
    elif template_id == "protocol":
        alias_map.update({
            _normalize_key("institution"): "INSTITUTION",
            _normalize_key("instytucja"): "INSTITUTION",
            _normalize_key("meeting_title"): "MEETING_TITLE",
            _normalize_key("title"): "MEETING_TITLE",
            _normalize_key("temat"): "MEETING_TITLE",
            _normalize_key("tytul"): "MEETING_TITLE",
            _normalize_key("meeting_date"): "MEETING_DATE",
            _normalize_key("date"): "MEETING_DATE",
            _normalize_key("data"): "MEETING_DATE",
            _normalize_key("meeting_time"): "MEETING_TIME",
            _normalize_key("time"): "MEETING_TIME",
            _normalize_key("czas"): "MEETING_TIME",
            _normalize_key("godzina"): "MEETING_TIME",
            _normalize_key("meeting_place"): "MEETING_PLACE",
            _normalize_key("place"): "MEETING_PLACE",
            _normalize_key("miejsce"): "MEETING_PLACE",
            _normalize_key("sala"): "MEETING_PLACE",
            _normalize_key("secretary"): "SECRETARY_NAME",
            _normalize_key("protokolant"): "SECRETARY_NAME",
            _normalize_key("sekretarz"): "SECRETARY_NAME",
            _normalize_key("chairperson"): "CHAIRPERSON_NAME",
            _normalize_key("prowadzacy"): "CHAIRPERSON_NAME",
            _normalize_key("przewodniczacy"): "CHAIRPERSON_NAME",
            _normalize_key("protocol_number"): "PROTOCOL_NUMBER",
            _normalize_key("numer"): "PROTOCOL_NUMBER",
            _normalize_key("attendees"): "ATTENDEES_LIST",
            _normalize_key("obecni"): "ATTENDEES_LIST",
            _normalize_key("uczestnicy"): "ATTENDEES_LIST",
            _normalize_key("lista_obecnosci"): "ATTENDEES_LIST",
            _normalize_key("agenda"): "AGENDA",
            _normalize_key("porzadek"): "AGENDA",
            _normalize_key("minutes"): "MEETING_MINUTES",
            _normalize_key("przebieg"): "MEETING_MINUTES",
            _normalize_key("opis"): "MEETING_MINUTES",
            _normalize_key("dyskusja"): "MEETING_MINUTES",
            _normalize_key("decisions"): "DECISIONS",
            _normalize_key("decyzje"): "DECISIONS",
            _normalize_key("ustalenia"): "DECISIONS",
            _normalize_key("actions"): "ACTION_ITEMS",
            _normalize_key("zadania"): "ACTION_ITEMS",
        })
    return alias_map


def normalize_template_params(template_id: str, parameters: dict) -> dict:
    """Map model-produced keys (e.g. "sender", "tytul") to the template's parameter names."""
    definitions = _load_templates_metadata().get("templates", {}).get(template_id, {}).get("parameters", {})
    alias_map = _param_alias_map(template_id, definitions)
    normalized: dict = {}
    for key, value in (parameters or {}).items():
        canonical = alias_map.get(_normalize_key(key))
        if canonical and canonical not in normalized:
            normalized[canonical] = value
    return normalized


def build_document_hitl_form(template_id: str, parameters: dict, templates_metadata: dict) -> dict:
    """HITL form for reviewing/editing document parameters before PDF generation."""
    template_info = templates_metadata.get("templates", {}).get(template_id, {})
    template_name = template_info.get("name", template_id)
    normalized = normalize_template_params(template_id, parameters or {})

    fields = []
    for param_name, param_def in template_info.get("parameters", {}).items():
        value = normalized.get(param_name, "")
        fields.append({
            "id": param_name,
            "label": param_def.get("description", param_name),
            "type": "textarea" if len(str(value)) > 100 else "text",
            "value": str(value),
            "required": param_def.get("required", True),
        })
    fields.insert(0, {"id": "_template_id", "label": "Szablon dokumentu", "type": "select", "value": template_id,
                      "options": list(templates_metadata.get("templates", {}).keys()), "required": True})

    return create_hitl_form_request(
        content=(f"📋 **Przegląd parametrów dokumentu: {template_name}**\n\n"
                 "Sprawdź i ewentualnie zmodyfikuj poniższe pola przed wygenerowaniem PDF."),
        fields=fields,
        continuation={"type": "generate_pdf", "template_id": template_id, "parameters": normalized},
    )
