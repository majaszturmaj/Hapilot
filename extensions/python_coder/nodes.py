"""Python Coder nodes — Code generation (Qwen/Bielik) and execution (Docker sandbox)."""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.llm_config import get_coder_chat
from src.core.tools_common import TaskState, invoke_llm_and_normalize


def run_coder(state):
    """Generate Python code from user request using the Coder model.

    Key improvements:
    - Sends MINIMAL context to Coder (only user request + error traceback on retry)
    - On retry, extracts actual traceback from state messages
    - Validates output: if LLM returned natural language instead of code, retries
    """
    from src.agent_graph import _resolve_effective_user_message

    print("   --> 💻 [NODE: CODER] Generowanie kodu Python (Qwen Coder)...")
    llm, system_prompt = get_coder_chat(role="coder")

    retries = state.get("retry_count", 0)
    original_user_request = _resolve_effective_user_message(state["messages"])

    # If executing a plan step, use step input
    task_plan = state.get("task_plan") or {}
    current_step = state.get("current_step")
    if task_plan and isinstance(task_plan, dict) and current_step is not None:
        steps = task_plan.get("steps", [])
        if 0 <= current_step < len(steps):
            step_input = steps[current_step].get("input")
            if step_input and not step_input.startswith("WYNIK:"):
                original_user_request = step_input

    # Build MINIMAL messages for Coder
    messages_to_send = [SystemMessage(content=system_prompt)]

    if retries > 0:
        error_traceback = ""
        previous_code = ""
        for msg in reversed(state["messages"]):
            content = msg.content if hasattr(msg, "content") else str(msg)
            if "BŁĄD WYKONANIA:" in content and not error_traceback:
                error_traceback = content.replace("WYNIK:\n", "").strip()
            if isinstance(msg, AIMessage) and not previous_code:
                if any(kw in content for kw in ["import ", "def ", "print(", "for ", "= "]):
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
        response = invoke_llm_and_normalize(llm, messages_to_send)

        # VALIDATION: Check if the response actually contains code
        resp_content = response.content if hasattr(response, "content") else str(response)
        from src.agent_tools import _extract_python_from_llm_output
        extracted = _extract_python_from_llm_output(resp_content)

        if not extracted.strip() or len(extracted.strip()) < 5:
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


def run_execute(state):
    """Execute Python code in the Docker sandbox."""
    from src.agent_tools import tool_execute_python
    from src.agent_graph import _advance_plan_step

    print("   --> ⚙️ [NODE: EXECUTE] Wykonywanie kodu Python...")

    # Prefer step input if present
    task_plan = state.get("task_plan") or {}
    current_step = state.get("current_step")
    content = None
    if task_plan and current_step is not None:
        steps = task_plan.get("steps", [])
        if 0 <= current_step < len(steps):
            content = steps[current_step].get("input")

    if content is None:
        last_message = state["messages"][-1]
        content = last_message.content

    result = tool_execute_python(content)
    print(f"      📝 [OUTPUT]:\n{result[:500]}...")

    status = "error" if "BŁĄD" in result else "success"

    # Persist state if needed
    task_id = state.get("task_id")
    if task_id and state.get("is_complex_task"):
        try:
            task_state = TaskState(task_id)
            task_state.update_step(state.get("retry_count", 0), result)
        except Exception:
            pass

    base_result = {
        "messages": [HumanMessage(content=f"WYNIK:\n{result}")],
        "execution_status": status,
        "retry_count": state.get("retry_count", 0) + 1,
    }
    if status == "success":
        base_result.update(_advance_plan_step(state))
    return base_result
