"""Document Modifier node — Modify existing LaTeX documents and recompile."""

import re
from langchain_core.messages import AIMessage, HumanMessage


def run_modify_document(state):
    """Modify an existing LaTeX document and recompile to PDF."""
    from src.agent_graph import _resolve_effective_user_message, _get_current_turn_ai_messages, _advance_plan_step
    from src.agent_tools import tool_modify_and_recompile_tex

    print("   --> 📝 [NODE: MODIFY DOCUMENT] Modyfikacja istniejącego dokumentu...")

    messages = state["messages"]
    last_user_msg = _resolve_effective_user_message(messages)

    tex_filename = state.get("last_generated_file") or ""

    # Plan step handling
    task_plan = state.get("task_plan") or {}
    current_step = state.get("current_step")
    modification_request = last_user_msg
    if task_plan and current_step is not None:
        steps = task_plan.get("steps", [])
        if 0 <= current_step < len(steps):
            step_input = steps[current_step].get("input")
            if step_input:
                modification_request = step_input

    # Gather context from current turn
    current_turn_msgs = _get_current_turn_ai_messages(messages)
    context_parts = []
    for m in current_turn_msgs:
        if isinstance(m, AIMessage) and m.content:
            context_parts.append(m.content)
    context = "\n---\n".join(context_parts[-3:]) if context_parts else ""

    result = tool_modify_and_recompile_tex(tex_filename, modification_request, context)
    status = "error" if "BŁĄD" in result else "success"

    base_result = {
        "messages": [HumanMessage(content=f"WYNIK:\n{result}")],
        "execution_status": status,
    }

    if status == "success":
        file_match = re.search(r'Wygenerowano plik:\s*(\S+\.pdf)', result)
        if file_match:
            pdf_name = file_match.group(1)
            tex_name = pdf_name.replace('.pdf', '.tex')
            base_result["last_generated_file"] = tex_name
        base_result.update(_advance_plan_step(state))
    else:
        base_result["current_step"] = None
    return base_result
