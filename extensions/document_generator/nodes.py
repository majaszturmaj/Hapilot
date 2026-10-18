"""Document Generator node — PDF documents from LaTeX templates, with a parameter review step (HITL).

1. First pass: the model picks a template and fills its parameters. Under the ``ask`` approval
   policy the user reviews them in a form and the run pauses; under ``auto`` the PDF is generated
   right away.
2. Second pass (after the form): the confirmed parameters are compiled to PDF and the plan goes on.
"""

import json
import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.core.tools_common import invoke_llm_and_normalize
from src.llm_config import get_bielik_chat


def _generate(state, template_id: str, parameters: dict) -> dict:
    from src.agent_graph import _advance_plan_step
    from src.agent_tools import tool_generate_pdf_from_template

    result = tool_generate_pdf_from_template(template_id, parameters)
    status = "error" if result.startswith("BŁĄD") else "success"
    out = {"messages": [HumanMessage(content=f"WYNIK:\n{result}")], "execution_status": status,
           "hitl_request": None, "hitl_response": None}
    if status == "success":
        match = re.search(r"Wygenerowano plik:\s*(\S+\.pdf)", result)
        if match:
            out["last_generated_file"] = match.group(1).replace(".pdf", ".tex")
        out.update(_advance_plan_step(state))
    else:
        out["current_step"] = None
    return out


def _error(text: str) -> dict:
    return {"messages": [HumanMessage(content=f"BŁĄD: {text}")], "execution_status": "error",
            "hitl_request": None, "current_step": None}


def run_document_generator(state):
    from src import run_context
    from src.agent_graph import (_contains_no_data_signal, _get_current_turn_ai_messages, _infer_template_hint,
                                 _resolve_effective_user_message)
    from src.agent_tools import (_get_condensed_templates_info, _load_templates_metadata, build_document_hitl_form,
                                 normalize_template_params)

    print("   --> 📄 [NODE: DOCUMENT GENERATOR] Generowanie dokumentu z szablonu...")

    # --- second pass: parameters confirmed in the form
    hitl_response = state.get("hitl_response")
    if hitl_response and hitl_response.get("continuation_type") == "generate_pdf":
        print("      ✅ [HITL] Odebrano potwierdzone parametry — generuję PDF...")
        if not hitl_response.get("template_id"):
            return _error("Brak template_id w odpowiedzi formularza.")
        return _generate(state, hitl_response["template_id"], hitl_response.get("parameters", {}))

    # --- first pass: choose a template and fill its parameters
    messages = state["messages"]
    last_user_msg = _resolve_effective_user_message(messages)
    template_hint = _infer_template_hint(last_user_msg)

    context_parts = [m.content for m in _get_current_turn_ai_messages(messages)
                     if isinstance(m, AIMessage) and m.content and not (template_hint and _contains_no_data_signal(m.content))]
    context = "\n---\n".join(context_parts[-3:])
    if len(context) > 1500:
        context = context[:1500] + "\n[...skrócono kontekst...]"

    user_request = last_user_msg
    task_plan = state.get("task_plan") or {}
    current_step = state.get("current_step")
    if task_plan and current_step is not None and 0 <= current_step < len(task_plan.get("steps", [])):
        step_input = task_plan["steps"][current_step].get("input")
        if step_input and last_user_msg:
            user_request = f"Oryginalne zapytanie: {last_user_msg} (Instrukcja do kroku: {step_input})"
            template_hint = _infer_template_hint(last_user_msg) or _infer_template_hint(step_input)
        elif step_input:
            user_request = step_input
            template_hint = _infer_template_hint(step_input)

    llm, system_prompt = get_bielik_chat(role="template_filler")
    forced = (f'WYMUSZENIE SZABLONU: Użytkownik jawnie poprosił o typ dokumentu "{template_hint}". '
              f'Użyj template_id dokładnie "{template_hint}".') if template_hint else ""
    prompt = f"""DOSTĘPNE SZABLONY:
{_get_condensed_templates_info()}

ZAPYTANIE UŻYTKOWNIKA: "{user_request}"

{forced}

{f'KONTEKST:{chr(10)}{context}' if context else ''}

Wybierz szablon i wypełnij WSZYSTKIE wymagane parametry. Treść po polsku, formalnym językiem.
Jeśli kontekst z RAG zawiera "brak danych", traktuj to jako kontekst pomocniczy i nadal wygeneruj dokument na podstawie zapytania użytkownika.
Dla list użyj \\\\begin{{enumerate}} \\\\item ... \\\\end{{enumerate}}

ZASADY WYBORU: (Priorytet!)
1. Jeśli użytkownik prosi o PODANIE, WNIOSEK, LIST -> "template_id: letter"
2. Jeśli użytkownik prosi o RAPORT, SPRAWOZDANIE, WYNIKI -> "template_id: report"
3. Jeśli użytkownik prosi o PROTOKÓŁ, NOTATKĘ ZE SPOTKANIA -> "template_id: protocol"

Odpowiedz WYŁĄCZNIE w formacie JSON:
{{
  "template_id": "letter|report|protocol",
  "parameters": {{
    "PARAM1": "wartość1",
    "PARAM2": "wartość2"
  }}
}}"""

    try:
        raw = invoke_llm_and_normalize(llm, [SystemMessage(content=system_prompt), HumanMessage(content=prompt)]).content
        if raw.startswith("[LLM"):
            return _error(f"Model nie może przetworzyć zapytania.\n{raw[:200]}")
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return _error(f"Model nie zwrócił JSON.\n{raw[:300]}")
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        return _error(f"Nieprawidłowy JSON: {exc}")
    except Exception as exc:
        return _error(str(exc))

    template_id = parsed.get("template_id")
    parameters = parsed.get("parameters", {}) or {}
    if template_hint:
        if template_id and template_id != template_hint:
            print(f"      ⚠️ [DOC GEN] Nadpisuję template_id modelu ({template_id}) -> {template_hint}")
        template_id = template_hint
    elif template_id not in _load_templates_metadata().get("templates", {}):
        lower = user_request.lower()
        if any(kw in lower for kw in ("raport", "sprawozdanie", "analiza")):
            template_id = "report"
        elif any(kw in lower for kw in ("protokół", "protokol", "spotkanie", "zebranie")):
            template_id = "protocol"
        else:
            template_id = "letter"
        print(f"      ⚠️ [DOC GEN] Model nie wybrał poprawnego szablonu — domyślnie: {template_id}")

    if run_context.approval_policy() == "auto":
        print(f"      ✅ [DOC GEN] Polityka auto — generuję PDF bez formularza (szablon={template_id})")
        from src import tracing

        tracing.event("approval", kind="document_form", policy="auto", decision="auto_approved",
                      summary=f"template {template_id}, {len(parameters)} parameters")
        return _generate(state, template_id, normalize_template_params(template_id, parameters))

    hitl_req = build_document_hitl_form(template_id, parameters, _load_templates_metadata())
    hitl_req["continuation"].update({"saved_plan": task_plan, "saved_step": current_step})
    print(f"      🛑 [HITL] Przygotowano formularz (szablon={template_id}, {len(parameters)} parametrów)")
    return {"messages": [AIMessage(content=f"Przygotowano parametry dokumentu ({template_id}). Oczekuję na potwierdzenie użytkownika...")],
            "execution_status": "pending_hitl", "hitl_request": hitl_req}
