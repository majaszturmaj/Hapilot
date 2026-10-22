"""LangGraph orchestration: task planner → router → extension nodes, built from loaded extensions.

Flow: ``task_planner`` asks the model for a JSON plan (steps with tools), applies a few
deterministic guards, and (for plans longer than two steps, under the ``ask`` approval policy)
asks the user to approve it. ``router`` maps the current step's tool to a node without calling a
model. Each node advances the plan; edges send the run to the next step's node or end it.

The compiled graph is built lazily on first use (``get_app()`` or the module attribute ``app``).
"""

from __future__ import annotations

import json
import operator
import re
import time
from typing import Annotated, List, Optional, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

from src import attachments, run_context, tracing
from src.agent_tools import TaskState, create_hitl_confirm_request, tool_plan_task
from src.config import get_settings
from src.llm_config import get_llm


class AgentState(TypedDict, total=False):
    messages: Annotated[List[BaseMessage], operator.add]
    next_step: str
    execution_status: str         # success / error / pending_hitl
    retry_count: int              # Python code repair attempts
    task_plan: dict               # plan produced by the task planner
    task_id: str                  # id for TaskState persistence
    plan_mode: str                # auto | explicit | off
    is_complex_task: bool         # multi-step plan
    current_step: Optional[int]   # index of the plan step being executed (None = no plan)
    last_generated_file: Optional[str]  # last generated .tex in this session's workspace
    generated_code: Optional[str]       # code produced by the coder node, run by the execute node
    hitl_request: Optional[dict]   # human-in-the-loop request for the UI
    hitl_response: Optional[dict]  # the user's answer to the previous HITL request
    halt: bool                     # planner decided there is nothing left to run


# =========================================================================== message helpers

RETRY_PATTERNS = [
    r"^spróbuj\s+(ponownie|jeszcze\s+raz)", r"^jeszcze\s+raz", r"^powtórz", r"^zrób\s+to\s+(ponownie|jeszcze|znowu)",
    r"^try\s+again", r"^retry", r"^ok,?\s*(spróbuj|zrób|powtórz)", r"^(kontynuuj|dalej|wznów)",
    r"^no\s+to\s+(spróbuj|zrób|lecimy|działaj)", r"^dawaj",
]


def _resolve_effective_user_message(messages) -> str:
    """The user's request; for a bare "try again" it is the previous substantive message."""
    human = [m for m in messages if isinstance(m, HumanMessage) and not m.content.lstrip().startswith("WYNIK:")]
    if not human:
        return messages[-1].content if messages else ""
    last = human[-1].content
    if not any(re.search(p, last.strip().lower()) for p in RETRY_PATTERNS):
        return last
    for msg in reversed(human[:-1]):
        if len(msg.content.strip()) > 15:
            print(f"🔄 [ROUTER] Wykryto retry: '{last}' → używam oryginalnego: '{msg.content[:80]}...'")
            return msg.content
    return last


def _get_current_turn_ai_messages(messages) -> list:
    """Messages produced during the current user turn (tool results 'WYNIK:' included)."""
    current = []
    for m in reversed(messages):
        if isinstance(m, HumanMessage) and not m.content.lstrip().startswith("WYNIK:"):
            break
        if getattr(m, "content", ""):
            current.append(m)
    current.reverse()
    return current


_PREVIOUS_ANSWER_PATTERNS = [
    r"\b(ten|tę|te|ta|to|tego|tej|tych)\s+(list\w*|pism\w*|podani\w*|wnios\w*|tekst\w*|treś\w*|dokument\w*|raport\w*|"
    r"protok\w*|notatk\w*|odpowied\w*|wiadomoś\w*|e-?mail\w*|maila?|szkic\w*|wersj\w*|sprawozdani\w*)",
    r"\b(to|tę|ten)\s+(jako|do|w)\b", r"\b(powyższ|poprzedni|wcześniejsz)\w*", r"\bz\s+(czatu|rozmowy)\b",
]


def _refers_to_previous_answer(text: str) -> bool:
    """True when the request points back at earlier output ("ten list", "to jako PDF", "powyższą treść")."""
    lower = (text or "").lower()
    return any(re.search(p, lower) for p in _PREVIOUS_ANSWER_PATTERNS)


def _get_previous_turn_answer(messages) -> str:
    """The assistant's last answer before the current user turn ("" if there is none)."""
    seen_user_turn = False
    for m in reversed(messages):
        if isinstance(m, HumanMessage) and not m.content.lstrip().startswith("WYNIK:"):
            seen_user_turn = True
        elif seen_user_turn and isinstance(m, AIMessage) and isinstance(m.content, str) and m.content.strip():
            return m.content
    return ""


NO_DATA_SIGNALS = [
    "brak informacji", "nie znaleziono", "brakuje szczegółowych", "nie zawiera", "brak procedur", "nie posiadam",
    "w dostępnych dokumentach brakuje", "dostarczone dokumenty nie zawierają", "nie znalazłem tej informacji",
    "brak wyników", "brak danych", "dokumenty skupiają się", "nie ma odpowiedzi",
]


def _contains_no_data_signal(text: str) -> bool:
    lower = (text or "").lower()
    return any(sig in lower for sig in NO_DATA_SIGNALS)


def _plan_has_pdf_like_step(task_plan: dict) -> bool:
    return any(isinstance(s, dict) and s.get("tool") in ("template_pdf", "latex", "modify_document")
               for s in (task_plan or {}).get("steps", []))


# =========================================================================== intent guards

# Edit verbs (word boundaries: "zmienna" or "poprawny" are not edit requests).
_EDIT_VERBS = re.compile(
    r"\b(zmie[nń](?:i[cć])?|popraw(?:i[cć])?|zaktualizuj|zmodyfikuj|skoryguj|edytuj|zamie[nń](?:i[cć])?|"
    r"usu[nń]|dodaj\s+(?:akapit|zdanie|punkt|sekcj[eę]))\b", re.IGNORECASE)
# Style changes count as document edits only when the message points at the document.
_STYLE_WORDS = re.compile(r"\b(inaczej|kr[oó]cej|pro[sś]ciej|d[lł]u[zż]ej|formalniej)\b", re.IGNORECASE)
_DOCUMENT_WORDS = re.compile(r"\b(dokument\w*|pism\w*|podani\w*|raport\w*|protok[oó][lł]\w*|pdf\w*|list\w*)\b",
                             re.IGNORECASE)

_DOC_REQUEST = re.compile(
    r"\b(pdf|wygeneruj|wydrukuj|podanie|wniosek|protok[oó][lł]|raport|pismo\s+formalne|list\s+formalny|"
    r"(?:przygotuj|stw[oó]rz|napisz|utw[oó]rz|sporz[aą]d[zź])\s+(?:mi\s+)?(?:dokument|raport|podanie|protok[oó][lł]|pismo|wniosek))\b",
    re.IGNORECASE)
# Stricter: an explicit request to create a document (a creation verb with a document noun, or with "PDF").
_DOC_EXPLICIT = re.compile(
    r"\b(?:przygotuj|stw[oó]rz|napisz|utw[oó]rz|sporz[aą]d[zź]|wygeneruj|zr[oó]b)(?:\s+mi)?\s+"
    r"(?:(?:dokument|raport|podanie|protok[oó][lł]|pismo|wniosek)\b|[^.?!\n]{0,60}?\bpdf\b)",
    re.IGNORECASE)
_DOC_NEGATION = re.compile(
    r"\b(?:nie|bez)\b[^.!?\n]{0,40}?\b(?:pdf\w*|dokument\w*|plik\w*|drukuj\w*)\b|\btylko\s+(?:tekst|w\s+czacie)\b|"
    r"\bw\s+oknie\s+czatu\b", re.IGNORECASE)


def is_modification_request(text: str, has_document: bool) -> bool:
    """A request to change an already generated document."""
    if not has_document or not text:
        return False
    if _EDIT_VERBS.search(text):
        return True
    return bool(_STYLE_WORDS.search(text) and _DOCUMENT_WORDS.search(text))


def wants_document(text: str) -> bool:
    """The user asks for a (PDF) document — and does not say they do not want one."""
    if not text or _DOC_NEGATION.search(text):
        return False
    return bool(_DOC_REQUEST.search(text))


def explicitly_wants_document(text: str) -> bool:
    """The user explicitly asks to create a document (stricter than ``wants_document``; used to add a PDF step)."""
    if not text or _DOC_NEGATION.search(text):
        return False
    return bool(_DOC_EXPLICIT.search(text))


def _infer_template_hint(text: str) -> Optional[str]:
    """letter / protocol / report when the request names the document type unambiguously."""
    lower = (text or "").lower()
    if any(re.search(r"\b" + kw + r"\b", lower) for kw in ["podanie", "wniosek", "list do", "pismo do", "oświadczenie", "rezygnacj"]):
        return "letter"
    if any(re.search(r"\b" + kw + r"\b", lower) for kw in ["protokół", "protokol", "notatkę ze", "notatka ze spotkania", "zebranie"]):
        return "protocol"
    if any(re.search(r"\b" + kw + r"\b", lower) for kw in ["raport", "sprawozdanie", "analizę", "analiza", "wyniki"]):
        return "report"
    return None


# =========================================================================== planner

def _approval_policy() -> str:
    return run_context.approval_policy()


def task_planner_node(state: AgentState):
    """Plan the task (or resume after a human-in-the-loop answer)."""
    hitl_response = state.get("hitl_response")
    if hitl_response:
        return _resume_after_hitl(state, hitl_response)

    messages = state["messages"]
    last_user_msg = _resolve_effective_user_message(messages)
    plan_mode = state.get("plan_mode") or get_settings().plan_mode
    if plan_mode == "off":
        return {"is_complex_task": False, "task_plan": {}, "current_step": None}

    print("📋 [NODE: TASK PLANNER] Analiza złożoności zadania...")
    history = []
    for m in (messages[-10:-1] if len(messages) > 1 else []):
        role = "System/Asystent" if isinstance(m, AIMessage) else "Użytkownik"
        content = getattr(m, "content", str(m))
        history.append(f"[{role}]: {content[:500] + ' [...skrócono]' if len(content) > 500 else content}")

    plan_json = tool_plan_task(last_user_msg, plan_mode=plan_mode, history_context="\n".join(history))
    task_id = f"task_{int(time.time() * 1000)}"
    try:
        task_plan = json.loads(plan_json) if isinstance(plan_json, str) else plan_json
        if not isinstance(task_plan, dict):
            raise ValueError("plan is not a JSON object")
    except Exception as exc:
        print(f"⚠️ Błąd planowania: {exc}. Kontynuuję bez planu.")
        return {"is_complex_task": False, "task_plan": {}, "current_step": None, "task_id": task_id}

    steps = [s for s in task_plan.get("steps", []) if isinstance(s, dict)]
    complexity = task_plan.get("complexity", "low")
    has_document = bool(state.get("last_generated_file"))
    # a file attached to this very message is what an edit request refers to, not the earlier generated PDF
    has_new_attachment = bool(attachments.names_in(last_user_msg))
    own_words = last_user_msg.split(attachments.LISTING_PREFIX, 1)[0]  # guards read the user's words, not attached text
    guards: list[str] = []
    tool_names = set(_build_dynamic_tool_map())  # guards for the built-in document tools apply only if they are loaded

    # Guard 1: an edit of an existing document is a single modify_document step.
    if ("modify_document" in tool_names and not has_new_attachment and is_modification_request(own_words, has_document)
            and not any(s.get("tool") == "modify_document" for s in steps)):
        print("📝 [PLANNER] Wykryto zapytanie o modyfikację dokumentu → modify_document")
        steps = [{"id": 1, "action": "Zmodyfikuj istniejący dokument zgodnie z instrukcjami użytkownika",
                  "tool": "modify_document", "input": last_user_msg}]
        complexity = "low"
        guards.append("forced_modify_document")
    # Guard 2: no new document unless the user asked for one (and did not say "no PDF").
    elif "template_pdf" in tool_names and not wants_document(own_words):
        kept = [s for s in steps if s.get("tool") != "template_pdf"]
        if len(kept) < len(steps):
            print(f"🔒 [PLANNER GUARD] Usunięto {len(steps) - len(kept)} krok(i) template_pdf — użytkownik nie prosił o dokument")
            guards.append(f"removed_template_pdf:{len(steps) - len(kept)}")
            steps = kept
            if not steps:
                steps = [{"id": 1, "action": "Odpowiedz na wiadomość", "tool": "llm_synthesis", "input": last_user_msg}]
    # Guard 3: an explicit request for a new document gets a document step if the plan has none.
    elif "template_pdf" in tool_names and explicitly_wants_document(own_words):
        tool_map = _build_dynamic_tool_map()
        if not any(tool_map.get(s.get("tool", "")) in (tool_map["template_pdf"], "modify_document") for s in steps):
            print("📄 [PLANNER GUARD] Użytkownik prosi o dokument — dodano krok template_pdf")
            steps = steps + [{"id": len(steps) + 1, "action": "Przygotuj dokument PDF, o który prosi użytkownik",
                              "tool": "template_pdf", "input": last_user_msg}]
            guards.append("added_template_pdf")
    # Guard 4: with no generated document, or with a new attachment (the edit is about that file), a
    # modify_document step next to other steps can only fail (seen with Bielik: file_edit, modify_document).
    if (not has_document or has_new_attachment) and len(steps) > 1 and any(s.get("tool") == "modify_document" for s in steps):
        kept = [s for s in steps if s.get("tool") != "modify_document"]
        guards.append(f"removed_modify_document:{len(steps) - len(kept)}")
        print("🔒 [PLANNER GUARD] Usunięto krok modify_document — w tej rozmowie nie ma dokumentu do zmiany")
        steps = kept
    # Guard 5: a confirmation with nothing after it asks about nothing — and its message would replace the
    # last real result (seen with Bielik: rag_search, template_pdf, hitl_confirm → the PDF link was lost).
    while len(steps) > 1 and steps[-1].get("tool") == "hitl_confirm":
        steps = steps[:-1]
        guards.append("removed_trailing_hitl_confirm")
        print("🔒 [PLANNER GUARD] Usunięto końcowy krok hitl_confirm — po nim nie ma już nic do potwierdzenia")
    task_plan["steps"] = steps
    task_plan["complexity"] = complexity if len(steps) > 1 else "low"
    is_complex = task_plan["complexity"] in ("medium", "high")
    print(f"📋 Plan: {task_plan['complexity'].upper()} ({len(steps)} kroków)")
    tracing.event("plan", task_plan=task_plan, guards_applied=guards)
    if len(steps) > 2 and _approval_policy() == "auto":
        tracing.event("approval", kind="plan", policy="auto", decision="auto_approved", summary=f"{len(steps)} steps")

    if len(steps) > 2 and _approval_policy() == "ask":
        lines = [f"  {s.get('id', i + 1)}. **{s.get('tool', '?')}** → {s.get('action', s.get('input', '?'))}"
                 for i, s in enumerate(steps)]
        hitl_req = create_hitl_confirm_request(
            content=(f"📋 **Plan zadania** ({task_plan.get('task_name', 'Zadanie')})\n"
                     f"Złożoność: {task_plan['complexity'].upper()} | Kroków: {len(steps)}\n\n"
                     + "\n".join(lines) + "\n\nCzy zatwierdzasz ten plan?"),
            continuation={"type": "approve_plan", "saved_plan": task_plan, "saved_is_complex": is_complex,
                          "saved_task_id": task_id},
        )
        print(f"🛑 [PLANNER] Plan ma {len(steps)} kroków — proszę użytkownika o zatwierdzenie.")
        return {"messages": [AIMessage(content="🛑 System przygotował plan zadania, który wymaga zatwierdzenia przez użytkownika.")],
                "is_complex_task": is_complex, "task_plan": task_plan, "task_id": task_id,
                "current_step": None, "hitl_request": hitl_req}

    return {"is_complex_task": is_complex, "task_plan": task_plan, "task_id": task_id,
            "current_step": 0 if steps else None}


def _resume_after_hitl(state: AgentState, hitl_response: dict) -> dict:
    """Continue a run after the user answered a HITL request (plan approval, confirmation, PDF form)."""
    kind = hitl_response.get("continuation_type", "")
    print(f"📋 [NODE: TASK PLANNER] Odpowiedź użytkownika ({kind or 'brak typu'}) — wznawiam.")
    saved_plan = hitl_response.get("saved_plan") or state.get("task_plan") or {}

    if kind == "approve_plan":
        if not hitl_response.get("confirmed", False):
            return {"is_complex_task": False, "task_plan": {}, "current_step": None, "hitl_response": None,
                    "hitl_request": None, "halt": True,
                    "messages": [AIMessage(content="Plan został anulowany przez użytkownika.")]}
        return {"is_complex_task": hitl_response.get("saved_is_complex", True), "task_plan": saved_plan,
                "task_id": hitl_response.get("saved_task_id", ""), "current_step": 0,
                "hitl_response": None, "hitl_request": None}

    if kind == "continue_plan":
        if not hitl_response.get("confirmed", False):
            return {"task_plan": {}, "current_step": None, "hitl_response": None, "hitl_request": None, "halt": True,
                    "messages": [AIMessage(content="Rozumiem — przerwałem wykonywanie planu.")]}
        next_index = (hitl_response.get("current_step") or 0) + 1
        if next_index >= len(saved_plan.get("steps", [])):
            return {"task_plan": {}, "current_step": None, "hitl_response": None, "hitl_request": None, "halt": True,
                    "messages": [AIMessage(content="✅ Potwierdzono. Plan został zakończony.")]}
        return {"is_complex_task": True, "task_plan": saved_plan, "current_step": next_index,
                "hitl_response": None, "hitl_request": None}

    if kind in ("generate_pdf", "resume_node"):
        target = "document_generator" if kind == "generate_pdf" else hitl_response.get("resume_node", "chat")
        return {"task_plan": saved_plan, "current_step": hitl_response.get("saved_step"),
                "is_complex_task": bool(saved_plan), "hitl_response": hitl_response, "hitl_request": None,
                "next_step": target}

    # unknown continuation: let the chat node answer
    return {"task_plan": {}, "current_step": None, "hitl_response": None, "hitl_request": None, "next_step": "chat"}


# =========================================================================== router

def router_node(state: AgentState):
    """Map the current plan step's tool to a graph node (no model call). Extension hooks may redirect."""
    hooked = _manager().before_route(dict(state))
    if hooked and hooked.get("next_step"):
        print(f"🔀 [ROUTER] Hook → {hooked['next_step']}")
        return {"retry_count": 0, **hooked}
    if state.get("hitl_response") and state.get("next_step"):
        target = state["next_step"]
        print(f"🔀 [ROUTER] Kontynuacja HITL → {target}")
        return {"next_step": target, "retry_count": 0, "hitl_request": None}

    task_plan = state.get("task_plan") or {}
    current_step = state.get("current_step")
    steps = task_plan.get("steps", []) if isinstance(task_plan, dict) else []
    if current_step is not None and 0 <= current_step < len(steps):
        tool = steps[current_step].get("tool", "")
        target = _build_dynamic_tool_map().get(tool, "chat")
        print(f"🔀 [ROUTER] Plan krok {current_step + 1}/{len(steps)}: tool={tool} → {target}")
        return {"next_step": target, "retry_count": 0, "is_complex_task": len(steps) > 1}

    print("🔀 [ROUTER] Brak planu — domyślnie kieruję do czatu")
    return {"next_step": "chat", "retry_count": 0, "is_complex_task": False, "task_plan": {}, "current_step": None}


# =========================================================================== plan progress

def _advance_plan_step(state: AgentState) -> dict:
    """Advance ``current_step`` after a node finished its step (call from nodes, not edges)."""
    task_plan = state.get("task_plan") or {}
    current_step = state.get("current_step")
    if not task_plan or not isinstance(task_plan, dict) or current_step is None:
        return {}
    steps = task_plan.get("steps", [])
    next_idx = current_step + 1
    if next_idx >= len(steps):
        print(f"      ✅ Plan zakończony (krok {current_step} był ostatni)")
        return {"current_step": None}

    last_ai = next((m.content for m in reversed(state.get("messages", [])) if isinstance(m, AIMessage)), None)
    if last_ai is None:
        last_ai = state["messages"][-1].content if state.get("messages") else ""
    if isinstance(steps[next_idx], dict):
        last_user_msg = _resolve_effective_user_message(state.get("messages", []))
        existing_input = steps[next_idx].get("input") or ""
        if not existing_input.strip() or existing_input == last_user_msg:
            steps[next_idx]["input"] = last_ai
        steps[next_idx]["_previous_output"] = last_ai

    task_id = state.get("task_id")
    if task_id and state.get("is_complex_task"):
        try:
            TaskState(task_id).update_context(f"step_{current_step}_output", last_ai)
        except Exception:
            pass
    print(f"      ➡️ Przechodzę do kroku {next_idx}/{len(steps)}")
    return {"current_step": next_idx, "task_plan": task_plan}


# =========================================================================== built-in nodes

def _fallback_chat(state: AgentState):
    """Minimal chat node used when the chat extension is disabled."""
    from src.core.tools_common import invoke_llm_and_normalize, to_valid_chat

    llm, system_prompt = get_llm("chat")
    resp = invoke_llm_and_normalize(llm, to_valid_chat([SystemMessage(content=system_prompt)] + state["messages"]))
    return {"messages": [resp], **_advance_plan_step(state)}


def run_hitl_confirm(state: AgentState):
    """Ask the user to confirm before the next plan step (auto-approved under the ``auto`` policy)."""
    task_plan = state.get("task_plan") or {}
    current_step = state.get("current_step")
    step = {}
    if task_plan and current_step is not None and 0 <= current_step < len(task_plan.get("steps", [])):
        step = task_plan["steps"][current_step]
    if _approval_policy() == "auto":
        print("   --> ✅ [NODE: HITL CONFIRM] Polityka auto — potwierdzam automatycznie.")
        tracing.event("approval", kind="confirm_step", policy="auto", decision="auto_approved",
                      summary=str(step.get("action") or step.get("input") or "")[:200])
        return {"messages": [AIMessage(content="(Potwierdzono automatycznie.)")], **_advance_plan_step(state)}

    print("   --> 🛑 [NODE: HITL CONFIRM] Oczekuję na potwierdzenie użytkownika...")
    description = step.get("action") or step.get("input") or "Potwierdź kontynuację."
    hitl_req = create_hitl_confirm_request(
        content=f"🛑 **Potwierdzenie wymagane**\n\n{description}\n\nCzy kontynuować?",
        continuation={"type": "continue_plan", "current_step": current_step, "saved_plan": task_plan},
    )
    return {"messages": [AIMessage(content="Oczekuję na potwierdzenie użytkownika.")], "hitl_request": hitl_req}


# =========================================================================== graph

def _manager():
    from src.core.extension_manager import get_active

    manager = get_active()
    if not manager.is_loaded():
        manager.discover_and_load()
    return manager


def _build_dynamic_tool_map() -> dict:
    """Planner tool name (or alias) → graph node, from the loaded extensions."""
    return _manager().tool_node_map()


def _instrument(name: str, func):
    """Wrap a node: deadline check, trace events, node name in the run context, hooks after the node."""
    import functools

    @functools.wraps(func)
    def node(state):
        tracing.check_deadline(f"node {name}")
        step = state.get("current_step")
        tracing.event("node_start", node=name, plan_step=step)
        started = time.monotonic()
        with run_context.use_context(node=name):
            try:
                out = func(state) or {}
            except Exception as exc:
                tracing.event("node_end", node=name, plan_step=step, duration_ms=round((time.monotonic() - started) * 1000),
                              status="exception", next=None, error=f"{type(exc).__name__}: {exc}")
                raise
        out = _manager().after_node(name, state, out)
        tracing.event("node_end", node=name, plan_step=step, duration_ms=round((time.monotonic() - started) * 1000),
                      status=out.get("execution_status") or ("hitl_request" if out.get("hitl_request") else "ok"),
                      next=out.get("next_step"))
        return out

    return node


def build_dynamic_graph(ext_manager=None):
    """Compile the LangGraph StateGraph from the loaded extensions.

    Every node continues according to its manifest: ``then`` (always that node), ``after.policy``
    ``plan`` (the next plan step's node, or end), ``on_success`` (as ``plan`` if the step succeeded,
    else end) or ``retry`` (as ``plan`` on success; otherwise back to ``retry_to`` until
    ``max_attempts`` is reached).
    """
    from src.core.extension_manager import NodeSpec, extension_manager, set_active

    ext_manager = ext_manager or extension_manager
    if not ext_manager.is_loaded():
        ext_manager.discover_and_load()
    set_active(ext_manager)

    specs = ext_manager.get_node_specs()
    if "chat" not in specs:
        specs["chat"] = NodeSpec(name="chat", extension_id="core", func=_fallback_chat)

    workflow = StateGraph(AgentState)
    workflow.add_node("task_planner", _instrument("task_planner", task_planner_node))
    workflow.add_node("router", _instrument("router", router_node))
    workflow.add_node("hitl_confirm", _instrument("hitl_confirm", run_hitl_confirm))
    for name, spec in specs.items():
        workflow.add_node(name, _instrument(name, spec.func))
    workflow.set_entry_point("task_planner")

    def planner_next(state: AgentState):
        return "end" if state.get("hitl_request") or state.get("halt") else "router"

    workflow.add_conditional_edges("task_planner", planner_next, {"router": "router", "end": END})

    known = set(specs) | {"hitl_confirm"}
    targets = {name: name for name in known}
    targets["end"] = END

    def next_from_plan(state: AgentState) -> str:
        current_step = state.get("current_step")
        steps = (state.get("task_plan") or {}).get("steps", [])
        if current_step is not None and 0 <= current_step < len(steps):
            mapped = _build_dynamic_tool_map().get(steps[current_step].get("tool", ""), "chat")
            return mapped if mapped in known else "chat"
        return "end"

    def route_from_router(state: AgentState) -> str:
        target = state.get("next_step") or "chat"
        return target if target in known else "chat"

    workflow.add_conditional_edges("router", route_from_router, dict(targets))

    def plan_policy(state: AgentState) -> str:
        return "end" if state.get("hitl_request") else next_from_plan(state)

    def on_success_policy(state: AgentState) -> str:
        if state.get("hitl_request") or state.get("execution_status") != "success":
            return "end"
        return next_from_plan(state)

    def retry_policy(max_attempts: int):
        def decide(state: AgentState) -> str:
            if state.get("execution_status") == "success":
                return next_from_plan(state)
            if state.get("retry_count", 0) >= max_attempts:
                print("      🛑 Przekroczono limit prób. Kończę.")
                return "end"
            print(f"      🔄 Wykryto błąd (próba {state.get('retry_count', 0)}/{max_attempts}) — ponawiam.")
            return "retry"
        return decide

    for name, spec in specs.items():
        if spec.then:
            workflow.add_edge(name, spec.then)
            continue
        policy = spec.after.get("policy", "plan")
        if policy == "on_success":
            workflow.add_conditional_edges(name, on_success_policy, dict(targets))
        elif policy == "retry":
            workflow.add_conditional_edges(name, retry_policy(int(spec.after.get("max_attempts", 3))),
                                           {**targets, "retry": spec.after["retry_to"]})
        else:
            workflow.add_conditional_edges(name, plan_policy, dict(targets))
    workflow.add_conditional_edges("hitl_confirm", plan_policy, dict(targets))

    compiled = workflow.compile()
    print(f"✅ [GRAPH] Compiled graph with {len(specs)} extension nodes + 3 core nodes")
    return compiled


_app = None


def get_app():
    """The compiled graph (built on first use)."""
    global _app
    if _app is None:
        from src.core.extension_manager import extension_manager

        print("🔌 [INIT] Discovering and loading extensions...")
        extension_manager.discover_and_load()
        _app = build_dynamic_graph(extension_manager)
    return _app


def reset_app() -> None:
    global _app
    _app = None


def rebuild_app():
    """Load the extensions again into a new manager and compile a new graph, then swap both in.

    Used by the admin panel after a custom node is added or deleted. A request that is already running
    keeps its compiled graph; its nodes then read the new manager (a superset or subset of the old one).
    Returns the new manager.
    """
    global _app
    import src.core.extension_manager as em

    manager = em.ExtensionManager()
    manager.discover_and_load()
    app = build_dynamic_graph(manager)  # also makes ``manager`` the active one
    em.extension_manager = manager
    _app = app
    return manager


def __getattr__(name: str):  # lazy module attribute ``app`` (backwards compatible)
    if name == "app":
        return get_app()
    raise AttributeError(name)
