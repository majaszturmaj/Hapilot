import operator
import re
import json
from typing import Annotated, List, TypedDict, Optional

from dotenv import load_dotenv
from langchain_core.messages import BaseMessage, SystemMessage, HumanMessage, AIMessage
from langgraph.graph import StateGraph, END

from src.llm_config import get_bielik_chat, get_coder_chat, PLAN_MODE, PlanMode
from src.agent_tools import (
    tool_rag_search, tool_check_calendar, tool_send_email_draft, 
    tool_execute_python, tool_generate_pdf, tool_plan_task, 
    tool_validate_document, TaskState, _extract_latex_from_llm_output,
    _generate_unique_filename,
    tool_list_templates, tool_generate_pdf_from_template,
    tool_select_template_and_generate,
    tool_read_sandbox_file, tool_modify_and_recompile_tex,
    SANDBOX_WORKSPACE,
    create_hitl_action_request, create_hitl_form_request,
    create_hitl_confirm_request, build_document_hitl_form,
    _load_templates_metadata, _get_condensed_templates_info,
)

load_dotenv()

# --- AGNET STATE DEFINITION ---
class AgentState(TypedDict):
    messages: Annotated[List[BaseMessage], operator.add]
    next_step: str
    execution_status: str  # success / error
    retry_count: int       # Licznik prób naprawy kodu
    task_plan: dict        # Zaplanowane kroki (do STORM)
    task_id: str          # ID dla persistencji stanu
    plan_mode: str        # auto | explicit | off
    is_complex_task: bool # Czy to zadanie wieloetapowe
    current_step: Optional[int]  # Indeks aktualnego kroku planu (None = brak planu)
    last_generated_file: Optional[str]  # Ostatnio wygenerowany plik (np. 'letter_20260305_173122.tex')
    hitl_request: Optional[dict]   # Human-in-the-loop: request for user input (type, content, fields/actions, continuation)
    hitl_response: Optional[dict]  # Human-in-the-loop: user's response from previous HITL interaction

# --- 2. WĘZEŁ ROUTERA ---
def _resolve_effective_user_message(messages) -> str:
    """Rozwiąż efektywną wiadomość użytkownika, uwzględniając kontekst konwersacji.
    
    Jeśli ostatnia wiadomość z user jest ogólnikowa (retry/follow-up),
    szukamy wcześniejszej, merytorycznej wiadomości i łączymy je.
    """
    last_user_msg = messages[-1].content
    
    # Wzorce wskazujące na ogólnikowy follow-up / retry
    RETRY_PATTERNS = [
        r"^spróbuj\s+(ponownie|jeszcze\s+raz)",
        r"^jeszcze\s+raz",
        r"^powtórz",
        r"^zrób\s+to\s+(ponownie|jeszcze|znowu)",
        r"^try\s+again",
        r"^retry",
        r"^OK,?\s*(spróbuj|zrób|powtórz)",
        r"^(kontynuuj|dalej|wznów)",
        r"^no\s+to\s+(spróbuj|zrób|lecimy|działaj)",
        r"^dawaj",
    ]
    
    msg_stripped = last_user_msg.strip().lower()
    
    is_retry = any(re.search(p, msg_stripped, re.IGNORECASE) for p in RETRY_PATTERNS)
    
    # Wiadomość jest zbyt krótka i nie ma treści merytorycznej
    if not is_retry and len(msg_stripped) > 15:
        return last_user_msg  # Wiadomość jest wystarczająco konkretna
    
    if not is_retry:
        return last_user_msg  # Krótka, ale nie wygląda na retry
    
    # Szukaj ostatniej merytorycznej wiadomości użytkownika (HumanMessage)
    for msg in reversed(messages[:-1]):
        if isinstance(msg, HumanMessage) and len(msg.content.strip()) > 15:
            original_msg = msg.content
            print(f"🔄 [ROUTER] Wykryto retry: '{last_user_msg}' → używam oryginalnego: '{original_msg[:80]}...'")
            return original_msg
    
    return last_user_msg


def _get_current_turn_ai_messages(messages) -> list:
    """Zwraca listę wiadomości wygenerowanych wyłącznie w ramach bieżącej sesji zapytania usera."""
    current_turn_msgs = []
    # Cofamy się w historii aż natrafimy na ostatnie "prawdziwe" zapytanie użytkownika
    # (ignorujemy sztuczne HumanMessage zawierające 'WYNIK:' z wykonania narzędzi).
    for m in reversed(messages):
        if isinstance(m, HumanMessage) and not m.content.lstrip().startswith("WYNIK:"):
            # To jest "główne" zapytanie użytkownika w obecnej turze
            break
        if getattr(m, "content", ""):
            current_turn_msgs.append(m)
    current_turn_msgs.reverse()
    return current_turn_msgs


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


def _infer_template_hint(text: str) -> Optional[str]:
    lower = (text or "").lower()
    
    # 1. Sprawdzenie jednoznacznych bezpośrednich sformułowań (Highest Priority - luźniejsze szukanie wyizolowanych słów)
    # Wyrzucamy wymóg słowa "napisz", szukając po prostu samego rdzenia "podanie" / "wniosek"
    strong_letter_kws = ["podanie", "wniosek", "list do", "pismo do", "oświadczenie", "rezygnacj"]
    if any(re.search(r'\b' + kw + r'\b', lower) for kw in strong_letter_kws):
        return "letter"
        
    strong_protocol_kws = ["protokół", "protokol", "notatkę ze", "notatka ze spotkania", "zebranie"]
    if any(re.search(r'\b' + kw + r'\b', lower) for kw in strong_protocol_kws):
        return "protocol"
        
    strong_report_kws = ["raport", "sprawozdanie", "analizę", "analiza", "wyniki", "rozpraw"]
    if any(re.search(r'\b' + kw + r'\b', lower) for kw in strong_report_kws):
        return "report"

    # 2. Punktacja heurystyczna (rezerwa dla mętnych zapytań)
    report_hits = sum(1 for kw in ["raport", "sprawozdanie", "analiza"] if kw in lower)
    protocol_hits = sum(1 for kw in ["protokół", "protokol", "spotkanie", "zebranie"] if kw in lower)
    letter_hits = sum(1 for kw in ["podanie", "list", "pismo", "wniosek"] if kw in lower)

    hits = {
        "report": report_hits,
        "protocol": protocol_hits,
        "letter": letter_hits, # 'letter' now contains higher priority phrases
    }
    
    # Podstawowe słowa jeśli nic konkretniejszego nie wpadło
    if sum(hits.values()) == 0:
        hits["letter"] += sum(1 for kw in ["podanie", "list", "pismo", "wniosek"] if kw in lower)
        hits["report"] += sum(1 for kw in ["analiza", "raport"] if kw in lower)

    best = max(hits, key=hits.get)
    if hits[best] == 0:
        return None

    # Zaufaj modelowi jeśli wyznacznik heurystyczny nie jest pewny
    top_count = hits[best]
    if sum(1 for v in hits.values() if v == top_count) > 1:
        return None
    return best


def router_node(state: AgentState):
    """Deterministic routing based on task_plan — no LLM call.

    The Task Planner already determines which tools to use via its JSON plan.
    This node simply maps the current step's tool to the corresponding graph node.
    """

    # HITL continuation: route directly to the target node from hitl_response
    hitl_response = state.get("hitl_response")
    if hitl_response:
        # The task_planner already resolved the target in next_step
        hitl_target = state.get("next_step") or "document_generator"
        print(f"🔀 [ROUTER] Kontynuacja HITL → {hitl_target}")
        return {
            "next_step": hitl_target,
            "retry_count": 0,
            "is_complex_task": False,
            "task_plan": {},
            "plan_mode": PLAN_MODE.value,
            "hitl_response": hitl_response,
            "hitl_request": None,
        }

    # Route based on the plan's current step tool
    task_plan = state.get("task_plan") or {}
    current_step = state.get("current_step")

    if task_plan and isinstance(task_plan, dict) and current_step is not None:
        steps = task_plan.get("steps", [])
        if 0 <= current_step < len(steps):
            step_tool = steps[current_step].get("tool", "")
            tool_map = _build_dynamic_tool_map()
            mapped = tool_map.get(step_tool, "chat")
            is_multi = len(steps) > 1
            print(f"🔀 [ROUTER] Plan krok {current_step + 1}/{len(steps)}: tool={step_tool} → {mapped}")
            return {
                "next_step": mapped,
                "retry_count": 0,
                "is_complex_task": is_multi,
                "task_plan": task_plan,
                "plan_mode": PLAN_MODE.value,
                "current_step": current_step,
            }

    # Fallback: no plan or empty plan → default to chat
    print("🔀 [ROUTER] Brak planu — domyślnie kieruję do czatu")
    return {
        "next_step": "chat",
        "retry_count": 0,
        "is_complex_task": False,
        "task_plan": {},
        "plan_mode": PLAN_MODE.value,
    }


def _normalize_llm_response(resp):
    """Ensure LLM invoke results are wrapped as BaseMessage-like objects.
    Some model adapters may return dicts; coerce them to AIMessage for downstream code.
    """
    from langchain_core.messages import AIMessage, SystemMessage
    if resp is None:
        return AIMessage(content="")
    # If it's already a message object, return as-is
    if hasattr(resp, "content"):
        return resp
    # If it's a dict, try to extract 'content'
    if isinstance(resp, dict):
        content = resp.get("content") or resp.get("text") or str(resp)
        return AIMessage(content=content)
    # Fallback: stringify
    return AIMessage(content=str(resp))

# --- 2B. WĘZEŁ PLANOWANIA ZADAŃ (NEW) ---
def task_planner_node(state: AgentState):
    """
    Rozbija złożone zapytania na strukturalne kroki (STORM-like).
    Wykonuje się przed routerem jeśli plan_mode=auto.

    When a hitl_response is present, skip planning entirely and pass through
    so the router can direct the response to the correct continuation node.
    """
    # HITL continuation: skip planning, let the router handle it
    hitl_response = state.get("hitl_response")
    if hitl_response:
        print("📋 [NODE: TASK PLANNER] HITL response detected — pomijam planowanie, kontynuuję z odpowiedzią użytkownika.")
        continuation_type = hitl_response.get("continuation_type", "")

        # --- Plan approval flow ---
        if continuation_type == "approve_plan":
            approved = hitl_response.get("confirmed", False)
            if not approved:
                print("📋 [NODE: TASK PLANNER] Użytkownik odrzucił plan.")
                return {
                    "is_complex_task": False,
                    "task_plan": {},
                    "current_step": None,
                    "hitl_response": None,
                    "hitl_request": None,
                    "messages": [AIMessage(content="Plan został anulowany przez użytkownika.")],
                }
            # User approved — restore the saved plan and start execution
            saved_plan = hitl_response.get("saved_plan", {})
            saved_complex = hitl_response.get("saved_is_complex", True)
            saved_task_id = hitl_response.get("saved_task_id", "")
            print(f"📋 [NODE: TASK PLANNER] Użytkownik zatwierdził plan ({len(saved_plan.get('steps', []))} kroków). Uruchamiam...")
            return {
                "is_complex_task": saved_complex,
                "task_plan": saved_plan,
                "task_id": saved_task_id,
                "current_step": 0,
                "hitl_response": None,
                "hitl_request": None,
            }

        # Map continuation types to graph targets
        target_map = {
            "generate_pdf": "document_generator",
            "send_email": "writer",
            "continue_plan": "chat",  # default fallback; router will re-route based on plan step
        }
        target = target_map.get(continuation_type, "document_generator")

        # For continue_plan, restore the plan context so router picks up the next step
        if continuation_type == "continue_plan":
            plan_step = hitl_response.get("current_step")
            if plan_step is not None:
                # Advance to next step after the hitl_confirm step
                next_step = plan_step + 1
                return {
                    "is_complex_task": True,
                    "task_plan": state.get("task_plan") or {},
                    "hitl_response": None,
                    "hitl_request": None,
                    "current_step": next_step,
                }

        return {
            "is_complex_task": False,
            "task_plan": {},
            "hitl_response": hitl_response,
            "hitl_request": None,
            "next_step": target,  # hint for router
        }

    messages = state["messages"]
    last_user_msg = _resolve_effective_user_message(messages)
    plan_mode = state.get("plan_mode", PLAN_MODE.value)
    
    # Sprawdź czy włączyć planowanie
    if plan_mode == "off":
        return {"is_complex_task": False, "task_plan": {}}
    
    print("📋 [NODE: TASK PLANNER] Analiza złożoności zadania...")
    
    # Przygotuj historię ostatnich kilkunastu wiadomości na wypadek gdyby 
    # użytkownik odnosił się do wcześniej wygenerowanych dokumentów.
    recent_msgs = messages[-10:-1] if len(messages) > 1 else []
    history_context = []
    for m in recent_msgs:
        role = "System/Asystent" if isinstance(m, AIMessage) else "Użytkownik"
        content = m.content if hasattr(m, "content") else str(m)
        if len(content) > 500:
            content = content[:500] + " [...skrócono]"
        history_context.append(f"[{role}]: {content}")
    history_str = "\n".join(history_context)

    # Generuj plan
    plan_json = tool_plan_task(last_user_msg, plan_mode=plan_mode, history_context=history_str)
    
    try:
        task_plan = json.loads(plan_json) if isinstance(plan_json, str) else plan_json
        complexity = task_plan.get("complexity", "low")
        steps = task_plan.get("steps", [])
        
        # Jeśli złożone i wymagane state persistence, przygotuj TaskState
        is_complex = complexity in ["medium", "high"]
        requires_persistence = task_plan.get("requires_state_persistence", False)
        
        task_id = f"task_{int(__import__('time').time() * 1000)}"
        
        if is_complex and requires_persistence:
            task_state = TaskState(task_id)
            print(f"💾 [PERSISTENCE] Utworzono state dla task_id={task_id}")
        
        print(f"📋 Plan: {complexity.upper()} ({len(steps)} kroków)")
        
        # Detect modification requests — if user wants to MODIFY an existing document,
        # force a single modify_document step regardless of what the LLM planned
        MODIFY_KEYWORDS = [
            "ZMODYFIKUJ", "ZMIEŃ", "POPRAW", "ZAKTUALIZUJ", "ZMIEN", "POPRAWIAJ",
            "SKORYGUJ", "ZMIEŃ DATĘ", "ZMIEŃ ADRES", "DODAJ AKAPIT",
            "ZMIEN DATE", "ZAMIEŃ", "EDYTUJ", "PRZERÓBK", "INACZEJ", "KRÓCEJ", "PROŚCIEJ"
        ]
        msg_upper = last_user_msg.upper()
        is_modification = any(k in msg_upper for k in MODIFY_KEYWORDS)
        
        if is_modification:
            # Check if any step already uses modify_document
            has_modify = any(s.get("tool") == "modify_document" for s in steps if isinstance(s, dict))
            if not has_modify:
                print("📝 [PLANNER] Wykryto zapytanie o modyfikację → wymuszam modify_document")
                task_plan["steps"] = [{
                    "id": 1,
                    "action": "Zmodyfikuj istniejący dokument zgodnie z instrukcjami użytkownika",
                    "tool": "modify_document",
                    "input": last_user_msg
                }]
                steps = task_plan["steps"]
                task_plan["complexity"] = "low"

        # Guard: strip template_pdf steps if user did NOT explicitly request a document/PDF.
        # This prevents the planner from generating PDF steps for pure knowledge questions.
        PDF_EXPLICIT_KEYWORDS = [
            "PDF", "RAPORT", "WYGENERUJ", "GENERUJ", "DRUKUJ",
            "PODANIE", "PROTOKÓŁ", "PROTOKOL", "PISMO FORMALNE",
            "LIST FORMALNY", "PRZYGOTUJ DOKUMENT", "STWÓRZ DOKUMENT",
            "NAPISZ DOKUMENT", "UTWÓRZ DOKUMENT", "NAPISZ PODANIE",
            "STWÓRZ RAPORT", "PRZYGOTUJ RAPORT", "NAPISZ RAPORT",
        ]
        wants_pdf = any(k in msg_upper for k in PDF_EXPLICIT_KEYWORDS)
        if not wants_pdf and not is_modification:
            original_count = len(steps)
            steps = [s for s in steps if isinstance(s, dict) and s.get("tool") != "template_pdf"]
            if len(steps) < original_count:
                removed = original_count - len(steps)
                print(f"🔒 [PLANNER GUARD] Usunięto {removed} krok(i) template_pdf — użytkownik nie prosił o dokument PDF")
                task_plan["steps"] = steps
                if len(steps) <= 1:
                    task_plan["complexity"] = "low"
                    is_complex = False
        
        # Fallback: jeśli model uznało zadanie za medium/high ale zwróciło <2 kroków,
        # wzmocnij plan dodając domyślną sekwencję: RAG -> Synthesis -> (opcjonalnie) LaTeX
        elif complexity in ["medium", "high"] and len(steps) < 2:
            print("⚠️ [PLANNER] Nudny plan (<2 kroków) dla złożonego zadania — stosuję fallback sequence.")
            fallback_steps = []

            # Heurystyka: czy to zadanie dotyczy redakcji/psania maila/tekstu (drafting)
            drafting_keywords = [
                "MAIL", "EMAIL", "DRAFUJ", "REDAGUJ", "ZREDAKUJ", "DZIĘK", "LIST",
                "PROSZĘ O", "PANI", "PANU", "SZANOWN", "DZIĘKCZ", "DZIĘKOW"
            ]

            is_drafting = any(k in msg_upper for k in drafting_keywords)
            wants_pdf = any(k in msg_upper for k in ["PDF", "RAPORT", "WYGENERUJ", "GENERUJ", "DRUKUJ"]) 

            if is_drafting:
                # Writer first (synthesize polite/dedicated text) then template_pdf for PDF
                fallback_steps.append({
                    "id": 1,
                    "action": "generate document text / draft email",
                    "tool": "llm_synthesis",
                    "input": last_user_msg
                })
                fallback_steps.append({
                    "id": 2,
                    "action": "select template and generate PDF from synthesized text",
                    "tool": "template_pdf",
                    "input": "Use the previous LLM output to fill the appropriate template and generate PDF."
                })
                if not wants_pdf:
                    # If they only wanted text (no PDF), treat second step as formatting/text output
                    fallback_steps[-1]["action"] = "format synthesized text"
            else:
                # Research-focused fallback: RAG -> synthesis -> optionally template_pdf
                fallback_steps.append({"id": 1, "action": "search documentation", "tool": "rag_search", "input": last_user_msg})
                fallback_steps.append({"id": 2, "action": "synthesize findings into text", "tool": "llm_synthesis", "input": "Use RAG results to produce a concise, referenced answer."})
                if wants_pdf:
                    fallback_steps.append({"id": 3, "action": "select template and generate PDF", "tool": "template_pdf", "input": "Use synthesized text to fill template and create PDF."})

            task_plan["steps"] = fallback_steps
            steps = fallback_steps

        # Jeśli mamy kroki, ustawiamy current_step=0, nawet dla pojedynczego kroku
        current_step = 0 if steps else None

        # --- HITL: User approval for plans with >2 steps ---
        if len(steps) > 2:
            plan_summary_lines = []
            for s in steps:
                tool_name = s.get("tool", "?")
                action_desc = s.get("action", s.get("input", "?"))
                plan_summary_lines.append(f"  {s.get('id', '?')}. **{tool_name}** → {action_desc}")
            plan_preview = "\n".join(plan_summary_lines)
            hitl_content = (
                f"📋 **Plan zadania** ({task_plan.get('task_name', 'Zadanie')})\n"
                f"Złożoność: {complexity.upper()} | Kroków: {len(steps)}\n\n"
                f"{plan_preview}\n\n"
                f"Czy zatwierdzasz ten plan?"
            )
            hitl_req = create_hitl_confirm_request(
                content=hitl_content,
                continuation={
                    "type": "approve_plan",
                    "saved_plan": task_plan,
                    "saved_is_complex": is_complex,
                    "saved_task_id": task_id,
                },
            )
            print(f"🛑 [PLANNER] Plan ma {len(steps)} kroków (>2) — proszę użytkownika o zatwierdzenie.")
            return {
                "messages": [AIMessage(content="🛑 System przygotował plan zadania, który wymaga zatwierdzenia przez użytkownika.")],
                "is_complex_task": is_complex,
                "task_plan": task_plan,
                "task_id": task_id,
                "current_step": None,  # not started yet
                "hitl_request": hitl_req,
            }

        return {
            "is_complex_task": is_complex,
            "task_plan": task_plan,
            "task_id": task_id,
            "current_step": current_step
        }
    except Exception as e:
        print(f"⚠️ Błąd planowania: {e}. Kontynuuję bez planu.")
        return {
            "is_complex_task": False,
            "task_plan": {},
            "task_id": f"task_{int(__import__('time').time() * 1000)}"
        }

# --- HELPER: Advance plan step (must be called from NODES, not edge functions) ---
def _advance_plan_step(state: AgentState) -> dict:
    """If executing a multi-step plan, advance current_step.
    
    Returns a dict with updated current_step (and task_plan with next step input populated).
    Must be called from node functions so LangGraph persists the state update.
    
    Returns:
        {"current_step": None} if not in a plan or at/past last step.
        {"current_step": next_idx, "task_plan": updated_plan} if advancing.
    """
    task_plan = state.get("task_plan") or {}
    current_step = state.get("current_step")
    
    if not task_plan or not isinstance(task_plan, dict) or current_step is None:
        return {}
    
    steps = task_plan.get("steps", [])
    next_idx = current_step + 1
    
    if next_idx >= len(steps):
        # This was the last step - mark plan as done
        print(f"      ✅ Plan zakończony (krok {current_step} był ostatni)")
        return {"current_step": None}
    
    # Capture last AI output to feed into the next step
    last_ai = None
    for m in reversed(state.get("messages", [])):
        if isinstance(m, AIMessage):
            last_ai = m.content
            break
    if last_ai is None:
        last_ai = state["messages"][-1].content if state.get("messages") else ""
    
    # Populate next step's input if not already set by the planner.
    # IMPORTANT: Don't overwrite planner-defined inputs with previous step output,
    # especially for multi-intent tasks where steps are independent (e.g. RAG + Python calc).
    if isinstance(steps[next_idx], dict):
        last_user_msg = _resolve_effective_user_message(state.get("messages", []))
        existing_input = steps[next_idx].get("input") or ""
        # Only propagate output if the step has NO input, or the input is generic/empty
        if not existing_input.strip() or existing_input == last_user_msg:
            steps[next_idx]["input"] = last_ai
        # Make previous output available as context without overwriting
        steps[next_idx]["_previous_output"] = last_ai
    
    # Persist intermediate output into TaskState context (if available)
    task_id = state.get("task_id")
    if task_id and state.get("is_complex_task"):
        try:
            ts = TaskState(task_id)
            ts.update_context(f"step_{current_step}_output", last_ai)
        except Exception:
            pass
    
    print(f"      ➡️ Przechodzę do kroku {next_idx}/{len(steps)}")
    return {"current_step": next_idx, "task_plan": task_plan}


# --- 3. NODE WRAPPERS (loaded from extensions) ---
# Extensions provide node functions. These wrappers ensure backward compatibility
# and allow extensions to import helper functions from this module.

def _load_extension_nodes():
    """Load node functions from extensions via the ExtensionManager.
    
    Falls back to built-in implementations if extension loading fails.
    Returns a dict of {node_name: callable}.
    """
    try:
        from src.core.extension_manager import extension_manager
        if not extension_manager.is_loaded():
            extension_manager.discover_and_load()
        return extension_manager.get_all_node_functions()
    except Exception as e:
        print(f"⚠️ [GRAPH] Extension loading failed: {e}. Using built-in fallbacks.")
        return {}


# --- Built-in fallback node implementations (used when extensions are not loaded) ---

def _fallback_chat(state):
    """Minimal chat fallback."""
    from src.core.tools_common import invoke_llm_and_normalize
    llm, system_prompt = get_bielik_chat(role="chat")
    resp = invoke_llm_and_normalize(llm, [SystemMessage(content=system_prompt)] + state["messages"])
    return {"messages": [resp]}


def _fallback_coder(state: AgentState):
    """Qwen/Bielik Coder generuje kod Python na podstawie zapytania użytkownika.
    
    Key improvements:
    - Sends MINIMAL context to Coder (only user request + error traceback on retry)
    - On retry, extracts actual traceback from state messages and shows it to model
    - Validates output: if LLM returned natural language instead of code, adds error
    """
    print("   --> 💻 [NODE: CODER] Generowanie kodu Python (Qwen Coder)...")
    llm, system_prompt = get_coder_chat(role="coder")
    
    retries = state.get("retry_count", 0)
    
    # Find the ORIGINAL user request (skip WYNIK: messages and planner noise)
    original_user_request = _resolve_effective_user_message(state["messages"])
    
    # If we're executing a step from a plan, use the step's input
    task_plan = state.get("task_plan") or {}
    current_step = state.get("current_step")
    if task_plan and isinstance(task_plan, dict) and current_step is not None:
        steps = task_plan.get("steps", [])
        if 0 <= current_step < len(steps):
            step_input = steps[current_step].get("input")
            if step_input and not step_input.startswith("WYNIK:"):
                original_user_request = step_input
    
    # Build MINIMAL messages for Coder — avoid flooding with planner/router context
    messages_to_send = [SystemMessage(content=system_prompt)]
    
    if retries > 0:
        # Extract the ACTUAL error traceback from recent messages
        error_traceback = ""
        previous_code = ""
        for msg in reversed(state["messages"]):
            content = msg.content if hasattr(msg, 'content') else str(msg)
            if "BŁĄD WYKONANIA:" in content and not error_traceback:
                error_traceback = content.replace("WYNIK:\n", "").strip()
            # Try to find previous code attempt from Coder's AIMessage
            if isinstance(msg, AIMessage) and not previous_code:
                if any(kw in content for kw in ['import ', 'def ', 'print(', 'for ', '= ']):
                    previous_code = content
        
        if error_traceback:
            messages_to_send.append(HumanMessage(
                content=f"Zadanie: {original_user_request}\n\n"
                        f"Poprzedni kod zwrócił błąd (próba {retries}/3).\n"
                        f"BŁĄD:\n{error_traceback}\n\n"
                        f"Napisz POPRAWIONY, KOMPLETNY kod Python w bloku ```python ... ```. "
                        f"NIE powtarzaj tego samego błędu."
            ))
        else:
            messages_to_send.append(HumanMessage(
                content=f"Zadanie: {original_user_request}\n\n"
                        f"Poprzednia próba nie zadziałała (próba {retries}/3). "
                        f"Napisz KOD PYTHON w bloku ```python ... ```.\n"
                        f"WAŻNE: Zwróć WYŁĄCZNIE kod, żadnego tekstu naturalnego."
            ))
    else:
        messages_to_send.append(HumanMessage(
            content=f"{original_user_request}\n\n"
                    f"Napisz kod Python w bloku ```python ... ```. "
                    f"Użyj print() aby wyświetlić wyniki."
        ))
    
    try:
        from src.agent_tools import invoke_llm_and_normalize
        response = invoke_llm_and_normalize(llm, messages_to_send)
        
        # VALIDATION: Check if the response actually contains code
        resp_content = response.content if hasattr(response, 'content') else str(response)
        from src.agent_tools import _extract_python_from_llm_output
        extracted = _extract_python_from_llm_output(resp_content)
        
        if not extracted.strip() or len(extracted.strip()) < 5:
            # Model returned natural language, not code
            print("      ⚠️ [CODER] Model nie zwrócił kodu Python — wymuszam ponowne generowanie")
            fallback_msg = HumanMessage(
                content=f"Poprzednia odpowiedź nie zawierała kodu Python.\n"
                        f"Zadanie: {original_user_request}\n\n"
                        f"Napisz KOD PYTHON w bloku ```python ... ```.\n"
                        f"Przykład poprawnej odpowiedzi:\n"
                        f"```python\nprint(42)\n```"
            )
            response = invoke_llm_and_normalize(llm, [
                SystemMessage(content=system_prompt), fallback_msg
            ])
        
        return {"messages": [response]}
    except Exception as e:
        return {"messages": [SystemMessage(content=f"Error: {e}")]}



# --- HITL Confirm Node (built-in, always available) ---

def run_hitl_confirm(state: AgentState):
    """Pause graph and ask user for confirmation (generic HITL step)."""
    print("   --> 🛑 [NODE: HITL CONFIRM] Oczekuję na potwierdzenie użytkownika...")
    task_plan = state.get("task_plan") or {}
    current_step = state.get("current_step")
    step_action = ""
    step_input = ""
    if task_plan and current_step is not None:
        steps = task_plan.get("steps", [])
        if 0 <= current_step < len(steps):
            step = steps[current_step]
            step_action = step.get("action", "")
            step_input = step.get("input", "")

    description = step_action or step_input or "Potwierdź kontynuację."
    from src.agent_tools import create_hitl_confirm_request
    hitl_req = create_hitl_confirm_request(
        content=f"🛑 **Potwierdzenie wymagane**\n\n{description}\n\nCzy kontynuować?",
        continuation={"type": "continue_plan", "current_step": current_step},
    )
    return {
        "messages": [AIMessage(content="Oczekuję na potwierdzenie użytkownika.")],
        "hitl_request": hitl_req,
    }


# ============================================================================ #
# 4. DYNAMIC GRAPH BUILDER                                                     #
# ============================================================================ #

def build_dynamic_graph(ext_manager=None):
    """Build and compile a LangGraph StateGraph from loaded extensions.

    This replaces the old static graph construction. Node functions are loaded
    from extensions, and edges are wired dynamically based on extension manifests.

    Args:
        ext_manager: An ExtensionManager instance (optional — uses singleton if None)

    Returns:
        Compiled LangGraph application ready for .invoke() or .stream().
    """
    from src.core.extension_manager import extension_manager as _em
    from src.core.route_registry import route_registry

    if ext_manager is None:
        ext_manager = _em

    # Ensure extensions are loaded
    if not ext_manager.is_loaded():
        ext_manager.discover_and_load()

    # Get all node functions from extensions
    ext_nodes = ext_manager.get_all_node_functions()

    # --- Build Workflow ---
    workflow = StateGraph(AgentState)

    # Always add core nodes
    workflow.add_node("task_planner", task_planner_node)
    workflow.add_node("router", router_node)
    workflow.add_node("hitl_confirm", run_hitl_confirm)

    # Add extension-provided nodes
    registered_node_names = set()
    for node_name, node_func in ext_nodes.items():
        workflow.add_node(node_name, node_func)
        registered_node_names.add(node_name)
        print(f"   ✅ [GRAPH] Added extension node: {node_name}")

    # Ensure essential fallback nodes exist if extensions didn't provide them
    fallback_map = {
        "chat": _fallback_chat,
        "coder": _fallback_coder,
    }
    for name, fallback in fallback_map.items():
        if name not in registered_node_names:
            workflow.add_node(name, fallback)
            registered_node_names.add(name)
            print(f"   ⚠️ [GRAPH] Added fallback node: {name}")

    # --- Entry Point ---
    workflow.set_entry_point("task_planner")

    # Task Planner -> Router (unless HITL approval is pending)
    def planner_next(state: AgentState):
        if state.get("hitl_request"):
            return "end"
        return "router"

    workflow.add_conditional_edges("task_planner", planner_next, {"router": "router", "end": END})

    # --- Router conditional edges ---
    # Build the mapping from next_step values to node names
    router_targets = {}
    for node_name in registered_node_names:
        router_targets[node_name] = node_name
    router_targets["hitl_confirm"] = "hitl_confirm"

    workflow.add_conditional_edges(
        "router",
        lambda x: x["next_step"],
        router_targets,
    )

    # --- Post-node edges ---
    # Determine edge patterns for each node based on extension manifests

    def check_document_result(state: AgentState):
        """Pure routing: step advancement done in the node."""
        if state.get("hitl_request"):
            return "end"
        if state.get("execution_status") != "success":
            return "end"
        current_step = state.get("current_step")
        if current_step is not None:
            task_plan = state.get("task_plan") or {}
            steps = task_plan.get("steps", [])
            if 0 <= current_step < len(steps):
                step_tool = steps[current_step].get("tool", "")
                tool_map = _build_dynamic_tool_map()
                mapped = tool_map.get(step_tool)
                if mapped:
                    return mapped
        return "end"

    def check_python_execution(state: AgentState):
        """Pure routing: step advancement done in the node."""
        if state.get("execution_status") == "success":
            current_step = state.get("current_step")
            if current_step is not None:
                task_plan = state.get("task_plan") or {}
                steps = task_plan.get("steps", [])
                if 0 <= current_step < len(steps):
                    step_tool = steps[current_step].get("tool", "")
                    tool_map = _build_dynamic_tool_map()
                    mapped = tool_map.get(step_tool)
                    if mapped:
                        return mapped
            return "end"
        if state.get("retry_count", 0) >= 3:
            print("      🛑 Przekroczono limit prób naprawy kodu. Kończę.")
            return "end"
        print(f"      🔄 Wykryto błąd! (Próba {state.get('retry_count', 0)}/3). Wracam do Codera...")
        return "retry"

    def continue_or_end(state: AgentState):
        """Generic: continue plan or end."""
        if state.get("hitl_request"):
            return "end"
        current_step = state.get("current_step")
        if current_step is not None:
            task_plan = state.get("task_plan") or {}
            if task_plan and isinstance(task_plan, dict):
                steps = task_plan.get("steps", [])
                if 0 <= current_step < len(steps):
                    step_tool = steps[current_step].get("tool", "")
                    tool_map = _build_dynamic_tool_map()
                    mapped = tool_map.get(step_tool)
                    if mapped:
                        return mapped
        return "end"

    # Wire edges for known node types
    doc_nodes = {"document_generator", "modify_document"}
    code_exec_nodes = {"execute"}
    code_gen_nodes = {"coder"}

    # Add 'end' edge mapping to all dynamic edge target lists
    router_targets["end"] = END

    for node_name in registered_node_names:
        if node_name in doc_nodes:
            workflow.add_conditional_edges(
                node_name, check_document_result,
                router_targets
            )
        elif node_name in code_exec_nodes:
            exec_targets = dict(router_targets)
            exec_targets["retry"] = "coder"
            workflow.add_conditional_edges(
                node_name, check_python_execution,
                exec_targets
            )
        elif node_name in code_gen_nodes:
            # Coder -> Execute
            if "execute" in registered_node_names:
                workflow.add_edge("coder", "execute")
        else:
            # Default: continue_or_end (knowledge, chat, writer, etc.)
            workflow.add_conditional_edges(
                node_name, continue_or_end,
                router_targets
            )

    # HITL confirm always goes to END
    workflow.add_edge("hitl_confirm", END)

    compiled = workflow.compile()
    print(f"✅ [GRAPH] Compiled graph with {len(registered_node_names)} extension nodes + 3 core nodes")
    return compiled


# --- Router node: Dynamic prompt composition ---

def _build_dynamic_router_prompt(last_user_msg: str) -> str:
    """Build the router classification prompt dynamically from registered routes."""
    from src.core.route_registry import route_registry

    routes = route_registry.get_all()
    if not routes:
        # Fallback to minimal routing
        return f"""Zaklasyfikuj wiadomość użytkownika.
Odpisz TYLKO literą C bez żadnego dodatkowego tekstu.

C) CZAT (Rozmowa ogólna)

WIADOMOŚĆ: "{last_user_msg}"

ODPOWIEDŹ (tylko jedna litera):"""

    # Build classification options
    options = []
    for route in routes:
        options.append(f"{route.id}) {route.description}")

    # Build examples
    examples = []
    for route in routes:
        if route.router_example:
            examples.append(f"- {route.router_example}")

    # Build letter range
    letters = sorted(set(r.id for r in routes))
    letter_range = ", ".join(letters)

    prompt = f"""Zaklasyfikuj wiadomość użytkownika.
Odpisz TYLKO literą {letter_range} bez żadnego dodatkowego tekstu.

{chr(10).join(options)}

WIADOMOŚĆ: "{last_user_msg}"

PRZYKŁADY:
{chr(10).join(examples)}

ODPOWIEDŹ (tylko jedna litera):"""
    return prompt


def _build_dynamic_tool_map() -> dict:
    """Build a tool-name to node-name mapping from extension manager."""
    try:
        from src.core.extension_manager import extension_manager
        from src.core.route_registry import route_registry

        tool_map = {}

        # Map tool names to their parent extension's node name
        for tool in extension_manager.get_all_tools():
            ext_id = tool.extension_id
            # Find the node name for this extension
            for manifest in extension_manager.get_all_manifests():
                if manifest.id == ext_id:
                    for node_data in manifest.nodes:
                        tool_map[tool.name] = node_data["name"]
                        break
                    break

        # Add common aliases
        tool_map.setdefault("rag_search", "knowledge")
        tool_map.setdefault("python", "coder")
        tool_map.setdefault("template_pdf", "document_generator")
        tool_map.setdefault("latex", "document_generator")
        tool_map.setdefault("coder", "coder")
        tool_map.setdefault("llm_synthesis", "writer")
        tool_map.setdefault("email", "writer")
        tool_map.setdefault("calendar", "knowledge")
        tool_map.setdefault("modify_document", "modify_document")
        tool_map.setdefault("read_previous_output", "modify_document")
        tool_map.setdefault("hitl_confirm", "hitl_confirm")
        tool_map.setdefault("chat", "chat")

        return tool_map
    except Exception:
        # Fallback hardcoded map
        return {
            "rag_search": "knowledge",
            "python": "coder",
            "template_pdf": "document_generator",
            "latex": "document_generator",
            "coder": "coder",
            "llm_synthesis": "writer",
            "email": "writer",
            "calendar": "knowledge",
            "modify_document": "modify_document",
            "read_previous_output": "modify_document",
            "hitl_confirm": "hitl_confirm",
            "chat": "chat",
        }


# ============================================================================ #
# 5. MODULE INITIALIZATION                                                      #
# ============================================================================ #

def _initialize_graph():
    """Initialize the extension system and build the graph.
    
    Called at module import time. Returns the compiled graph application.
    """
    try:
        from src.core.extension_manager import extension_manager
        print("🔌 [INIT] Discovering and loading extensions...")
        extension_manager.discover_and_load()
        print("🔨 [INIT] Building dynamic graph...")
        return build_dynamic_graph(extension_manager)
    except Exception as e:
        print(f"⚠️ [INIT] Extension system failed: {e}")
        import traceback
        traceback.print_exc()
        print("⚠️ [INIT] Falling back to minimal graph...")
        # Build a minimal graph with just chat
        workflow = StateGraph(AgentState)
        workflow.add_node("task_planner", task_planner_node)
        workflow.add_node("router", router_node)
        workflow.add_node("chat", _fallback_chat)
        workflow.add_node("hitl_confirm", run_hitl_confirm)
        workflow.set_entry_point("task_planner")
        workflow.add_conditional_edges("task_planner", 
            lambda s: "end" if s.get("hitl_request") else "router", 
            {"router": "router", "end": END})
        workflow.add_conditional_edges("router", lambda x: x["next_step"], 
            {"chat": "chat", "hitl_confirm": "hitl_confirm"})
        workflow.add_conditional_edges("chat", 
            lambda s: "end", {"end": END})
        workflow.add_edge("hitl_confirm", END)
        return workflow.compile()


# Build the graph at import time (same behavior as before)
app = _initialize_graph()
