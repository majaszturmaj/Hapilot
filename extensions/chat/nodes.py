"""Chat node — General conversation handler."""

from langchain_core.messages import SystemMessage
from src.llm_config import get_bielik_chat
from src.core.tools_common import invoke_llm_and_normalize


def run_chat(state):
    """Handle general conversation queries."""
    print("   --> 💬 [NODE: CHAT]")
    llm, system_prompt = get_bielik_chat(role="chat")
    resp = invoke_llm_and_normalize(
        llm, [SystemMessage(content=system_prompt)] + state["messages"]
    )
    result = {"messages": [resp]}

    # Advance plan step if in a multi-step plan
    from src.agent_graph import _advance_plan_step
    result.update(_advance_plan_step(state))
    return result
