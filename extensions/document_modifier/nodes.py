"""Document Modifier node — change a document generated earlier in this session and recompile it."""

import re

from langchain_core.messages import AIMessage, HumanMessage


def run_modify_document(state):
    from src.agent_graph import _advance_plan_step, _get_current_turn_ai_messages, _resolve_effective_user_message
    from src.agent_tools import tool_modify_document

    print("   --> 📝 [NODE: MODIFY DOCUMENT] Modyfikacja istniejącego dokumentu...")
    messages = state["messages"]
    request = _resolve_effective_user_message(messages)
    task_plan = state.get("task_plan") or {}
    current_step = state.get("current_step")
    if task_plan and current_step is not None and 0 <= current_step < len(task_plan.get("steps", [])):
        request = task_plan["steps"][current_step].get("input") or request

    context = "\n---\n".join([m.content for m in _get_current_turn_ai_messages(messages)
                              if isinstance(m, AIMessage) and m.content][-3:])
    result = tool_modify_document(state.get("last_generated_file") or "", request, context)
    status = "error" if result.startswith("BŁĄD") else "success"

    out = {"messages": [HumanMessage(content=f"WYNIK:\n{result}")], "execution_status": status}
    if status == "success":
        match = re.search(r"Wygenerowano plik:\s*(\S+\.pdf)", result)
        if match:
            out["last_generated_file"] = match.group(1).replace(".pdf", ".tex")
        out.update(_advance_plan_step(state))
    else:
        out["current_step"] = None
    return out
