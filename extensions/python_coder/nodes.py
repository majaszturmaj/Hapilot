"""Python Coder nodes — code generation (coder role) and execution in the isolated sandbox."""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.core.tools_common import TaskState, invoke_llm_and_normalize
from src.llm_config import get_coder_chat


def _task_text(state) -> str:
    """What the code should do: the plan step's input, else the user's request."""
    from src.agent_graph import _resolve_effective_user_message

    request = _resolve_effective_user_message(state["messages"])
    task_plan = state.get("task_plan") or {}
    current_step = state.get("current_step")
    if task_plan and isinstance(task_plan, dict) and current_step is not None:
        steps = task_plan.get("steps", [])
        if 0 <= current_step < len(steps):
            step_input = steps[current_step].get("input")
            if step_input and not step_input.startswith("WYNIK:"):
                request = step_input
    return request


def _conversation_text(messages, requests: int = 3, request_chars: int = 400, code_chars: int = 3000) -> str:
    """Earlier requests and the last code of this conversation — a follow-up like "wykres jest pusty" or "dane są
    w kolejności ID, nazwa, wartość…" means nothing without them (seen on the pilot)."""
    from src import attachments
    from src.agent_graph import _get_current_turn_ai_messages

    current = {id(m) for m in _get_current_turn_ai_messages(messages)}
    users = [m.content.split(attachments.LISTING_PREFIX, 1)[0].strip() for m in messages
             if isinstance(m, HumanMessage) and not m.content.lstrip().startswith("WYNIK:")][:-1]  # not the current one
    code = next((m.content for m in reversed(messages)
                 if isinstance(m, AIMessage) and id(m) not in current and "```python" in str(m.content)), "")
    parts = []
    if users[-requests:]:
        parts.append("WCZEŚNIEJSZE PROŚBY UŻYTKOWNIKA W TEJ ROZMOWIE (od najstarszej):\n" + "\n".join(
            f"- {u[:request_chars]}{'…' if len(u) > request_chars else ''}" for u in users[-requests:] if u))
    if code:
        block = code[code.index("```python"):]
        parts.append("OSTATNI KOD PYTHON W TEJ ROZMOWIE (popraw go, jeśli prośba tego dotyczy):\n"
                     + (block if len(block) <= code_chars else block[:code_chars] + "\n[…]"))
    return "\n\n".join(parts)


def _context_text(state, limit: int = 6000) -> str:
    """Files the code can read (with the structure of data files), earlier requests and code of this conversation,
    and the results of earlier plan steps (e.g. what the knowledge search found)."""
    from src.agent_graph import _get_current_turn_ai_messages
    from src.agent_tools import files_for_code

    parts = []
    files = files_for_code()
    if files:
        parts.append("PLIKI DOSTĘPNE DLA KODU:\n" + files)
    conversation = _conversation_text(state["messages"])
    if conversation:
        parts.append(conversation)
    earlier = [m.content for m in _get_current_turn_ai_messages(state["messages"])
               if getattr(m, "content", "") and not (isinstance(m, AIMessage) and "```python" in m.content)]
    if earlier:
        text = "\n---\n".join(earlier)
        parts.append("DANE Z POPRZEDNICH KROKÓW:\n" + (text[-limit:] if len(text) > limit else text))
    return "\n\n".join(parts)


def run_coder(state):
    """Generate Python code for the task; the code is handed to ``run_execute`` via state."""
    from src.agent_tools import _extract_python_from_llm_output

    print("   --> 💻 [NODE: CODER] Generowanie kodu Python...")
    llm, system_prompt = get_coder_chat(role="coder")
    retries = state.get("retry_count", 0)
    task = _task_text(state)
    context = _context_text(state)
    if context:
        task = f"{task}\n\n{context}"

    if retries > 0:
        error = next((m.content.replace("WYNIK:\n", "").strip() for m in reversed(state["messages"])
                      if "BŁĄD WYKONANIA:" in getattr(m, "content", "") or "BŁĄD:" in getattr(m, "content", "")), "")
        previous = state.get("generated_code") or ""
        content = (f"Zadanie: {task}\n\nPoprzedni kod (próba {retries}/3):\n```python\n{previous}\n```\n\n"
                   f"zwrócił błąd:\n{error}\n\nNapisz POPRAWIONY, KOMPLETNY kod Python w bloku ```python ... ```. "
                   "NIE powtarzaj tego samego błędu.")
    else:
        content = f"{task}\n\nNapisz kod Python w bloku ```python ... ```. Użyj print() aby wyświetlić wyniki."

    try:
        response = invoke_llm_and_normalize(llm, [SystemMessage(content=system_prompt), HumanMessage(content=content)])
        code = _extract_python_from_llm_output(getattr(response, "content", ""))
        if len(code.strip()) < 5:
            print("      ⚠️ [CODER] Model nie zwrócił kodu Python — ponawiam z przykładem formatu")
            response = invoke_llm_and_normalize(llm, [
                SystemMessage(content=system_prompt),
                HumanMessage(content=f"Poprzednia odpowiedź nie zawierała kodu Python.\nZadanie: {task}\n\n"
                                     "Napisz KOD PYTHON w bloku ```python ... ```.\nPrzykład:\n```python\nprint(42)\n```"),
            ])
            code = _extract_python_from_llm_output(getattr(response, "content", ""))
        return {"messages": [response], "generated_code": code}
    except Exception as exc:
        return {"messages": [AIMessage(content=f"Błąd generowania kodu: {exc}")], "generated_code": ""}


def run_execute(state):
    """Run the code produced by ``run_coder`` in the sandbox."""
    from src.agent_graph import _advance_plan_step
    from src.agent_tools import tool_execute_python

    print("   --> ⚙️ [NODE: EXECUTE] Wykonywanie kodu Python...")
    code = state.get("generated_code")
    if code is None:  # older callers: the coder's answer is the last AI message
        code = next((m.content for m in reversed(state["messages"]) if isinstance(m, AIMessage)), "")
    result = tool_execute_python(code)
    print(f"      📝 [OUTPUT]:\n{result[:500]}")
    status = "error" if result.startswith("BŁĄD") else "success"

    task_id = state.get("task_id")
    if task_id and state.get("is_complex_task"):
        try:
            TaskState(task_id).update_step(state.get("retry_count", 0), result)
        except Exception:
            pass

    out = {"messages": [HumanMessage(content=f"WYNIK:\n{result}")], "execution_status": status,
           "retry_count": state.get("retry_count", 0) + 1}
    if status == "success":
        out.update(_advance_plan_step(state))
    return out
