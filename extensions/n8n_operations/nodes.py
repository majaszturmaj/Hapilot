"""Optional n8n operations: Outlook e-mail DRAFTS (never sent) and calendar look-ups.

Off by default (``"enabled": false`` in the manifest; the n8n container runs only with the
``n8n`` compose profile). The webhook base URL comes from ``HAPILOT_N8N_WEBHOOK_BASE`` and must be
local. A draft is created only after the user confirms it — never automatically, not even under the
``auto`` approval policy. No credentials live in this repository: n8n holds them.
"""

import re
from datetime import date, timedelta

import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.core.tools_common import create_hitl_confirm_request, extract_json_object, invoke_llm_and_normalize


def _webhook(path: str) -> str:
    from src.config import assert_local_url, get_settings

    base = get_settings().n8n_webhook_base
    assert_local_url(base)
    return f"{base}/{path}"


def _step(state) -> dict:
    plan = state.get("task_plan") or {}
    idx = state.get("current_step")
    steps = plan.get("steps", [])
    return steps[idx] if idx is not None and 0 <= idx < len(steps) else {}


def parse_date(text: str, today: date) -> date:
    """A date mentioned in Polish text (ISO, DD.MM.YYYY, dziś/jutro/pojutrze); default: today."""
    lower = (text or "").lower()
    if m := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", lower):
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if m := re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", lower):
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    if "pojutrze" in lower:
        return today + timedelta(days=2)
    if "jutr" in lower:
        return today + timedelta(days=1)
    return today


def _calendar(state) -> dict:
    from src import run_context
    from src.agent_graph import _advance_plan_step, _resolve_effective_user_message

    text = _step(state).get("input") or _resolve_effective_user_message(state["messages"])
    day = parse_date(text, run_context.today())
    try:
        resp = httpx.post(_webhook("outlook-calendar"), json={"date": day.isoformat()}, timeout=10.0)
        resp.raise_for_status()
        events = resp.json().get("events") or []
        listing = "\n".join(f"- {e}" for e in events) if events else "Brak wydarzeń."
        content = f"Kalendarz na {day.isoformat()}:\n{listing}"
    except Exception as exc:
        content = f"BŁĄD: Nie udało się odczytać kalendarza przez n8n: {exc}"
    return {"messages": [AIMessage(content=content)], **_advance_plan_step(state)}


def _email(state) -> dict:
    from src import run_context
    from src.agent_graph import _advance_plan_step, _resolve_effective_user_message
    from src.llm_config import get_llm

    response = state.get("hitl_response") or {}
    if response.get("resume_node") == "operations":
        draft = response.get("draft") or {}
        if not response.get("confirmed"):
            return {"messages": [AIMessage(content="Rozumiem — nie utworzyłem wersji roboczej.")], "hitl_response": None,
                    **_advance_plan_step(state)}
        try:
            r = httpx.post(_webhook("outlook-draft"), json=draft, timeout=10.0)
            r.raise_for_status()
            content = f"✅ Utworzono wersję roboczą w Outlooku (nie została wysłana). ID: {r.json().get('draft_id', '?')}"
        except Exception as exc:
            content = f"BŁĄD: Nie udało się utworzyć wersji roboczej przez n8n: {exc}"
        return {"messages": [AIMessage(content=content)], "hitl_response": None, **_advance_plan_step(state)}

    request = _step(state).get("input") or _resolve_effective_user_message(state["messages"])
    llm, system_prompt = get_llm("writer")
    raw = invoke_llm_and_normalize(llm, [SystemMessage(content=system_prompt), HumanMessage(content=(
        f"Przygotuj wersję roboczą e-maila na podstawie polecenia:\n{request}\n\n"
        'Odpowiedz WYŁĄCZNIE JSON: {"to": "adres lub puste", "subject": "temat", "body": "treść"}'))]).content
    draft = extract_json_object(raw or "") or {}
    draft = {"to": str(draft.get("to", "")), "subject": str(draft.get("subject", "")), "body": str(draft.get("body", raw))}
    preview = f"**Do:** {draft['to'] or '(uzupełnij w Outlooku)'}\n**Temat:** {draft['subject']}\n\n{draft['body']}"

    if run_context.approval_policy() == "auto":
        from src import tracing

        tracing.event("approval", kind="email_draft", policy="auto", decision="not_created",
                      summary="drafts are created only after a user's confirmation")
        return {"messages": [AIMessage(content=f"Wersja robocza e-maila (nie utworzono jej w Outlooku bez potwierdzenia):\n\n{preview}")],
                **_advance_plan_step(state)}
    hitl = create_hitl_confirm_request(
        content=f"📧 **Utworzyć wersję roboczą w Outlooku?** (nic nie zostanie wysłane)\n\n{preview}",
        continuation={"type": "resume_node", "resume_node": "operations", "draft": draft,
                      "saved_plan": state.get("task_plan") or {}, "saved_step": state.get("current_step")},
    )
    return {"messages": [AIMessage(content="Oczekuję na potwierdzenie utworzenia wersji roboczej e-maila.")], "hitl_request": hitl}


def run_operations(state):
    tool = _step(state).get("tool", "")
    if tool == "calendar":
        return _calendar(state)
    return _email(state)
