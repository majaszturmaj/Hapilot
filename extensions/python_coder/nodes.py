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


def _context_text(state, limit: int = 6000) -> str:
    """Files the code can read and the results of earlier plan steps (e.g. what the knowledge search found)."""
    from src.agent_graph import _get_current_turn_ai_messages
    from src.agent_tools import files_for_code

    parts = []
    files = files_for_code()
    if files:
        parts.append("PLIKI DOSTĘPNE DLA KODU:\n" + files)
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
