"""Custom nodes created by users in the agent panel (src/admin.py): one model call with the node's own prompt.

A custom node is an ordinary extension in the custom extensions folder. Its ``nodes.py`` only calls
``run_custom_node(state, "<node>")``; everything else comes from the manifest: the planner tool that
sends steps to it, the system prompt (role = node name, editable in the admin panel) and ``config``:
``model_role`` ("default" or "coder": which model of the session's profile answers) and ``temperature``.

The node answers the current plan step (or the user's message) with the results of the earlier steps
of this request and the recent conversation as context, then continues with the plan. It runs no code
and calls no tools.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

CONTEXT_LIMIT = 6000      # characters of earlier results passed to the model
HISTORY_MESSAGES = 6      # recent conversation messages passed to the model


def _config(node_name: str) -> dict:
    from src.core.extension_manager import get_active

    manager = get_active()
    spec = manager.get_node_specs().get(node_name)
    return manager.get_config(spec.extension_id) if spec else {}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[...skrócono...]"


def build_request(state: dict) -> str:
    """The text the custom node answers: the plan step, earlier results of this request, recent conversation."""
    from src.agent_graph import _get_current_turn_ai_messages, _resolve_effective_user_message

    messages = state.get("messages") or []
    user_message = _resolve_effective_user_message(messages) or ""
    task_plan = state.get("task_plan") or {}
    current = state.get("current_step")
    steps = task_plan.get("steps", []) if isinstance(task_plan, dict) else []
    step = steps[current] if current is not None and 0 <= current < len(steps) else {}

    parts = [f"Prośba użytkownika: {user_message}"]
    if step.get("action") or step.get("input"):
        parts.append(f"Zadanie tego kroku: {step.get('action') or ''} {step.get('input') or ''}".strip())
    results = [m.content for m in _get_current_turn_ai_messages(messages) if getattr(m, "content", "")]
    results += [m.content for m in messages[-4:] if isinstance(m, HumanMessage) and m.content.lstrip().startswith("WYNIK:")]
    if results:
        parts.append("Wyniki wcześniejszych kroków:\n" + _clip("\n---\n".join(results), CONTEXT_LIMIT))
    history = []
    for m in messages[-HISTORY_MESSAGES - 1:-1]:
        who = "Asystent" if isinstance(m, AIMessage) else "Użytkownik"
        history.append(f"{who}: {_clip(getattr(m, 'content', ''), 800)}")
    if history:
        parts.append("Ostatnie wiadomości rozmowy:\n" + "\n".join(history))
    return "\n\n".join(parts)


def run_custom_node(state: dict, node_name: str) -> dict:
    from src.agent_graph import _advance_plan_step
    from src.core.tools_common import invoke_llm_and_normalize
    from src.llm_config import get_llm

    config = _config(node_name)
    print(f"   --> 🟣 [NODE: {node_name}] węzeł własny")
    llm, system_prompt = get_llm(node_name, model_role=config.get("model_role") or "default",
                                 temperature=config.get("temperature"))
    answer = invoke_llm_and_normalize(llm, [SystemMessage(content=system_prompt),
                                            HumanMessage(content=build_request(state))])
    status = "error" if answer.content.startswith("[LLM") else "success"
    out = {"messages": [AIMessage(content=answer.content)], "execution_status": status}
    out.update(_advance_plan_step(state) if status == "success" else {"current_step": None})
    return out
