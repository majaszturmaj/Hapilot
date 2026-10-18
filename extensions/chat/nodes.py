"""Chat node — general conversation handler."""

from langchain_core.messages import SystemMessage

from src.core.tools_common import invoke_llm_and_normalize, to_valid_chat
from src.llm_config import get_bielik_chat


def run_chat(state):
    """Answer conversationally. Inside a plan, the step's instruction closes the conversation."""
    from src.agent_graph import _advance_plan_step

    print("   --> 💬 [NODE: CHAT]")
    llm, system_prompt = get_bielik_chat(role="chat")
    step = {}
    plan, idx = state.get("task_plan") or {}, state.get("current_step")
    if plan and idx is not None and 0 <= idx < len(plan.get("steps", [])):
        step = plan["steps"][idx]
    instruction = step.get("action") or step.get("input") or None
    messages = to_valid_chat([SystemMessage(content=system_prompt)] + list(state["messages"]), instruction)
    resp = invoke_llm_and_normalize(llm, messages)
    return {"messages": [resp], **_advance_plan_step(state)}
