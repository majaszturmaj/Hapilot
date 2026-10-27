"""RAG Knowledge node — Search company documentation and synthesize answers."""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.llm_config import get_bielik_chat
from src.core.tools_common import TaskState, invoke_llm_and_normalize


# These imports are deferred to avoid circular dependencies at module load time.
# The actual tool functions live in src/agent_tools.py for now (backwards compat).

NO_DATA_SIGNALS = [
    "brak informacji", "nie znaleziono", "brakuje szczegółowych",
    "nie zawiera", "brak procedur", "nie posiadam",
    "w dostępnych dokumentach brakuje", "dostarczone dokumenty nie zawierają",
    "nie znalazłem tej informacji", "brak wyników", "brak danych",
    "dokumenty skupiają się", "nie ma odpowiedzi",
]


def _contains_no_data_signal(text: str) -> bool:
    lower = (text or "").lower()
    return any(sig in lower for sig in NO_DATA_SIGNALS)


def _plan_has_pdf_like_step(task_plan: dict) -> bool:
    steps = (task_plan or {}).get("steps", [])
    for step in steps:
        if isinstance(step, dict) and step.get("tool") in ["template_pdf", "latex", "modify_document"]:
            return True
    return False


def run_knowledge(state):
    """Search company documentation via RAG and synthesize an answer."""
    print("   --> 📂 [NODE: KNOWLEDGE] Analiza zapytania...")
    from src.agent_tools import tool_rag_search
    from src.agent_graph import _resolve_effective_user_message, _advance_plan_step

    messages = state["messages"]
    last_user_msg = _resolve_effective_user_message(messages)

    task_plan = state.get("task_plan") or {}
    has_pdf_step = _plan_has_pdf_like_step(task_plan)
    current_step = state.get("current_step")
    if task_plan and current_step is not None:
        steps = task_plan.get("steps", [])
        if 0 <= current_step < len(steps):
            step_input = steps[current_step].get("input")
            if step_input:
                last_user_msg = step_input

    # Build conversation history context
    history_context = []
    recent_msgs = messages[-12:-1] if len(messages) > 1 else []
    for m in recent_msgs:
        if isinstance(m, HumanMessage):
            if not m.content.strip().startswith("WYNIK:"):
                history_context.append(f"Użytkownik: {m.content}")
        elif isinstance(m, AIMessage):
            content = m.content
            if len(content) > 300:
                content = content[:300] + " [...skrócono]"
            history_context.append(f"Asystent: {content}")
    history_str = "\n".join(history_context) if history_context else "Brak historii."

    # 1. Query reformulation for RAG
    print("      🧠 Optymalizuję zapytanie dla RAG...")
    llm, system_prompt = get_bielik_chat(role="knowledge")

    rewrite_prompt = f"""Twoim zadaniem jest zamienić pytanie użytkownika na słowa kluczowe do wyszukiwarki.
Usuń zbędne słowa. Zostaw tylko to co istotne (np. nazwy własne, tematy). Jeśli użytkownik prosi o zmianę wcześniejszego tekstu (np. "krócej", "napisz inaczej"), opierając się na DANE Z HISTORII wyciągnij CZEGO DOTYCZYŁ ten tekst i użyj jako słów docelowych.

DANE Z HISTORII:
{history_str}

PYTANIE DO PRZEFORMUŁOWANIA: "{last_user_msg}"
Słowa kluczowe:"""

    try:
        optimized_query_msg = invoke_llm_and_normalize(
            llm, [SystemMessage(content=system_prompt), HumanMessage(content=rewrite_prompt)]
        )
        optimized_query = optimized_query_msg.content.strip()
    except Exception:
        optimized_query = last_user_msg

    print(f"      🔍 Szukam w plikach: '{optimized_query}'")

    # 2. Knowledge-base search
    if not optimized_query or optimized_query.startswith("[LLM"):
        optimized_query = last_user_msg
    tool_output = tool_rag_search(optimized_query)

    # 3. Synthesis
    if has_pdf_step:
        synthesis_prompt = f"""DANE Z HISTORII KONWERSACJI:
{history_str}

Oto treść dokumentów firmowych znaleziona dla hasła: "{optimized_query}".

DOKUMENTY:
{tool_output}

To zadanie jest częścią planu, który kończy się wygenerowaniem PDF.
RAG jest tutaj WYŁĄCZNIE pomocniczy (procedury, styl formalny, kontekst organizacyjny).
Jeśli dokumenty nie zawierają danych merytorycznych, NIE odmawiaj i NIE kończ odpowiedzią typu "brak danych".
Zamiast tego przygotuj zwięzły kontekst roboczy do dalszego pisania dokumentu na podstawie opisu użytkownika i wiedzy z historii.
Odpowiedz użytkownikowi na polecenie/pytanie: "{last_user_msg}".
Źródła cytuj tylko wtedy, gdy rzeczywiście są przydatne.
"""
    else:
        synthesis_prompt = f"""DANE Z HISTORII KONWERSACJI:
{history_str}

Oto treść dokumentów firmowych znaleziona dla hasła: "{optimized_query}".

DOKUMENTY:
{tool_output}

Odpowiedz użytkownikowi na jego najnowsze polecenie/pytanie: "{last_user_msg}".
Bądź naturalny, korzystaj z historii do zrozumienia kontekstu (szczególnie jeśli w DOKUMENTY nic nie było, albo polecenie było prośbą o zmianę np. 'napisz to prościej').
Zawsze cytuj źródła z dokumentów, jeśli się na nich opierasz.
Jeśli w dokumentach i historii nie ma odpowiedzi, przyznaj że nie znalazłeś tej informacji w udostępnionych plikach.
"""

    final_response = invoke_llm_and_normalize(
        llm, [SystemMessage(content=system_prompt), HumanMessage(content=synthesis_prompt)]
    )

    # Persist state if needed
    task_id = state.get("task_id")
    if task_id and state.get("is_complex_task"):
        try:
            task_state = TaskState(task_id)
            task_state.update_context("knowledge_sources", tool_output)
        except Exception:
            pass

    # Handle no-data scenarios
    response_lower = final_response.content.lower()
    response_length = len(final_response.content.strip())
    no_data_detected = _contains_no_data_signal(response_lower)

    if no_data_detected and has_pdf_step:
        print("ℹ️ [KNOWLEDGE] Brak danych RAG, ale plan zawiera PDF — kontynuuję generowanie z danych użytkownika.")
        final_response = AIMessage(content=(
            "KONTEKST RAG (pomocniczy): Nie znaleziono merytorycznych danych o tym konkretnym temacie "
            "w bazie firmowej. To NIE blokuje zadania. Przygotuj treść dokumentu na podstawie informacji "
            "podanych przez użytkownika, zachowując formalny styl."
        ))
        response_lower = final_response.content.lower()
        response_length = len(final_response.content.strip())

    if no_data_detected and state.get("is_complex_task") and response_length < 300 and not has_pdf_step:
        print("⚠️ [KNOWLEDGE] Brak danych w RAG — przerywam plan wieloetapowy (abort remaining steps)")
        return {"messages": [final_response], "current_step": None}
    elif no_data_detected and state.get("is_complex_task") and not has_pdf_step:
        print(f"ℹ️ [KNOWLEDGE] Sygnał 'brak danych' wykryty, ale odpowiedź jest obszerna ({response_length} znaków) — kontynuuję plan.")

    result = {"messages": [final_response]}
    result.update(_advance_plan_step(state))
    return result


# --------------------------------------------------------------------------- list of documents

def _plural(n: int, one: str, few: str, many: str) -> str:
    if n == 1:
        return one
    return few if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else many


def document_list_text(docs: list[dict], attached: list[str]) -> str:
    """The knowledge base's documents (from the index) and the files attached in this chat, as an answer."""
    from src.ingest_data import ADDED_PREFIX

    if not docs:
        lines = ["📚 Baza wiedzy jest pusta — nie ma w niej jeszcze żadnych dokumentów."]
    else:
        lines = [f"📚 **W bazie wiedzy {_plural(len(docs), 'jest', 'są', 'jest')} {len(docs)} "
                 f"{_plural(len(docs), 'dokument', 'dokumenty', 'dokumentów')}:**", ""]
        for i, d in enumerate(docs, 1):
            source = d["source"]
            added = source.startswith(ADDED_PREFIX + "/")
            name = source.split("/", 1)[1] if added else source
            if d["pages"]:
                detail = f"{d['pages']} {_plural(d['pages'], 'strona', 'strony', 'stron')}"
                if d["ocr_pages"]:
                    detail += (f", w tym {d['ocr_pages']} "
                               f"{_plural(d['ocr_pages'], 'odczytana', 'odczytane', 'odczytanych')} ze skanu")
            elif d["sheets"]:
                detail = f"{_plural(len(d['sheets']), 'arkusz', 'arkusze', 'arkuszy')}: {', '.join(d['sheets'])}"
            else:
                detail = f"{d['chunks']} {_plural(d['chunks'], 'fragment', 'fragmenty', 'fragmentów')}"
            lines.append(f"{i}. **{name}** — {detail}" + (" (dodany z czatu)" if added else ""))
    if attached:
        lines += ["", "📎 **Pliki załączone w tej rozmowie:** " + ", ".join(attached)]
    lines += ["", "Odpowiadając na pytanie, szukam w tych dokumentach fragmentów, które do niego pasują, i podaję źródło."]
    return "\n".join(lines)


def run_document_list(state):
    """List the documents the knowledge search can find — read from the index, no model, no search."""
    print("   --> 📚 [NODE: DOCUMENT LIST] Spis dokumentów w bazie wiedzy")
    from src import attachments, knowledge_base
    from src.agent_graph import _advance_plan_step

    attached = attachments.session_attachments(state["messages"])[1]
    text = document_list_text(knowledge_base.documents(), attached)
    return {"messages": [AIMessage(content=text)], **_advance_plan_step(state)}
