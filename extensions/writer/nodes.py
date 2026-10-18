"""Writer node — Text synthesis and content generation."""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from src.llm_config import get_bielik_chat
from src.core.tools_common import invoke_llm_and_normalize


def _plan_has_pdf_like_step(task_plan: dict) -> bool:
    """Check if the task plan contains a PDF/document generation step."""
    steps = (task_plan or {}).get("steps", [])
    for step in steps:
        if isinstance(step, dict) and step.get("tool") in ["template_pdf", "latex", "modify_document"]:
            return True
    return False


def _get_current_turn_ai_messages(messages) -> list:
    """Return AI messages generated only during the current user query turn."""
    current_turn_msgs = []
    for m in reversed(messages):
        if isinstance(m, HumanMessage) and not m.content.lstrip().startswith("WYNIK:"):
            break
        if getattr(m, "content", ""):
            current_turn_msgs.append(m)
    current_turn_msgs.reverse()
    return current_turn_msgs


def run_writer(state):
    """Run Bielik as a writer/synthesizer for plan steps that require textual synthesis."""
    print("   --> ✍️ [NODE: WRITER] Generowanie treści...")
    llm, system_prompt = get_bielik_chat(role="writer")

    task_plan = state.get("task_plan") or {}
    current_step = state.get("current_step")
    step_input = None
    step_action = None
    step_tool = ""
    if task_plan and current_step is not None:
        steps = task_plan.get("steps", [])
        if 0 <= current_step < len(steps):
            step = steps[current_step]
            step_input = step.get("input")
            step_action = step.get("action")
            step_tool = step.get("tool", "")

    # Build a writing prompt that includes relevant conversation history
    prompt_text = ""
    if step_action:
        prompt_text += f"Zadanie: {step_action}\n"
    if step_input:
        prompt_text += f"Input: {step_input}\n"
    if not prompt_text:
        prompt_text = state["messages"][-1].content
    if step_tool == "email":
        # Without the optional n8n extension nothing can be sent: the writer only drafts the text.
        prompt_text += ("\nPrzygotuj treść e-maila jako WERSJĘ ROBOCZĄ do skopiowania. "
                        "Nie twierdź, że wiadomość została wysłana — Hapilot niczego nie wysyła.\n")

    # Check if plan contains PDF/modification step
    has_pdf_step = _plan_has_pdf_like_step(task_plan)

    # If plan HAS PDF generation → isolate context (cut conversation history amnesia).
    # Otherwise (plain conversation) deliver history so assistant remembers what it just generated.
    if has_pdf_step:
        relevant_msgs = _get_current_turn_ai_messages(state.get("messages", []))
    else:
        relevant_msgs = state.get("messages", [])[-12:]

    history_context = []
    for m in relevant_msgs:
        if isinstance(m, AIMessage):
            role = "Asystent:"
            content = m.content if hasattr(m, "content") else str(m)
            if len(content) > 2000:
                content = content[:2000] + "\n[...skrócono...]"
            history_context.append(f"{role}\n{content}")
        elif isinstance(m, HumanMessage) and not has_pdf_step:
            role = "Użytkownik:"
            content = m.content if hasattr(m, "content") else str(m)
            history_context.append(f"{role}:\n{content}")

    if history_context:
        prompt_text = (
            "DANE MERYTORYCZNE ORAZ HISTORIA SESJI:\n"
            + "\n---\n".join(history_context)
            + "\n=====================\n\n"
            + prompt_text
        )

    try:
        response = invoke_llm_and_normalize(
            llm, [SystemMessage(content=system_prompt), HumanMessage(content=prompt_text)]
        )
        result = {"messages": [response]}
        from src.agent_graph import _advance_plan_step
        result.update(_advance_plan_step(state))
        return result
    except Exception as e:
        return {"messages": [SystemMessage(content=f"Error (writer): {e}")], "current_step": None}
