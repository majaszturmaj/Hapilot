"""Document Generator node — PDF generation from LaTeX templates with HITL."""

import re
import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.llm_config import get_bielik_chat
from src.core.tools_common import invoke_llm_and_normalize


def run_document_generator(state):
    """Generate PDF documents from LaTeX templates.

    HITL flow:
    1. First invocation (no hitl_response): LLM selects template & fills params,
       then creates a HITL form request for user confirmation. Graph ends.
    2. Second invocation (hitl_response present): Uses confirmed/edited params
       to generate PDF directly.
    """
    from src.agent_tools import (
        _load_templates_metadata, _get_condensed_templates_info,
        tool_generate_pdf_from_template, build_document_hitl_form,
    )
    from src.agent_graph import (
        _resolve_effective_user_message, _get_current_turn_ai_messages,
        _infer_template_hint, _contains_no_data_signal, _advance_plan_step,
    )

    print("   --> 📄 [NODE: DOCUMENT GENERATOR] Generowanie dokumentu z szablonu...")

    # --- HITL Phase 2: User already confirmed/edited parameters ---
    hitl_response = state.get("hitl_response")
    if hitl_response and hitl_response.get("continuation_type") == "generate_pdf":
        print("      ✅ [HITL] Odebrano potwierdzone parametry — generuję PDF...")
        template_id = hitl_response.get("template_id")
        parameters = hitl_response.get("parameters", {})

        if not template_id:
            return {
                "messages": [HumanMessage(content="BŁĄD: Brak template_id w odpowiedzi HITL.")],
                "execution_status": "error",
                "hitl_request": None,
                "hitl_response": None,
            }

        result = tool_generate_pdf_from_template(template_id, parameters)
        status = "error" if "BŁĄD" in result else "success"

        base_result = {
            "messages": [HumanMessage(content=f"WYNIK:\n{result}")],
            "execution_status": status,
            "hitl_request": None,
            "hitl_response": None,
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

    # --- HITL Phase 1: Prepare parameters and ask user for confirmation ---
    messages = state["messages"]
    last_user_msg = _resolve_effective_user_message(messages)
    template_hint = _infer_template_hint(last_user_msg)

    # Gather context from current turn
    current_turn_msgs = _get_current_turn_ai_messages(messages)
    context_parts = []
    for m in current_turn_msgs:
        if isinstance(m, AIMessage) and m.content:
            if template_hint and _contains_no_data_signal(m.content):
                continue
            context_parts.append(m.content)
    context = "\n---\n".join(context_parts[-3:]) if context_parts else ""

    # Plan step handling
    task_plan = state.get("task_plan") or {}
    current_step = state.get("current_step")
    user_request = last_user_msg
    if task_plan and current_step is not None:
        steps = task_plan.get("steps", [])
        if 0 <= current_step < len(steps):
            step_input = steps[current_step].get("input")
            if step_input and last_user_msg:
                user_request = f"Oryginalne zapytanie: {last_user_msg} (Instrukcja do kroku: {step_input})"
                step_hint = _infer_template_hint(step_input)
                orig_hint = _infer_template_hint(last_user_msg)
                template_hint = orig_hint if orig_hint else step_hint
            elif step_input:
                user_request = step_input
                template_hint = _infer_template_hint(step_input)

    # LLM fills template parameters
    templates_summary = _get_condensed_templates_info()
    max_context_chars = 1500
    if context and len(context) > max_context_chars:
        context = context[:max_context_chars] + "\n[...skrócono kontekst...]"

    llm, system_prompt = get_bielik_chat(role="template_filler")

    prompt = f"""DOSTĘPNE SZABLONY:
{templates_summary}

ZAPYTANIE UŻYTKOWNIKA: "{user_request}"

{f'WYMUSZENIE SZABLONU: Użytkownik jawnie poprosił o typ dokumentu "{template_hint}". Użyj template_id dokładnie "{template_hint}".' if template_hint else ''}

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
        response = invoke_llm_and_normalize(
            llm, [SystemMessage(content=system_prompt), HumanMessage(content=prompt)]
        )
        raw_response = response.content

        if raw_response.startswith("[LLM"):
            return {
                "messages": [HumanMessage(content=f"BŁĄD: Model nie może przetworzyć zapytania.\n{raw_response[:200]}")],
                "execution_status": "error",
                "hitl_request": None,
                "current_step": None,
            }

        json_match = re.search(r'\{.*\}', raw_response, re.DOTALL)
        if not json_match:
            return {
                "messages": [HumanMessage(content=f"BŁĄD: Model nie zwrócił JSON.\n{raw_response[:300]}")],
                "execution_status": "error",
                "hitl_request": None,
                "current_step": None,
            }

        parsed = json.loads(json_match.group(0))
        template_id = parsed.get("template_id")
        parameters = parsed.get("parameters", {})

        # Force template if user clearly requested a specific type
        if template_hint:
            if template_id and template_id != template_hint:
                print(f"      ⚠️ [DOC GEN] Nadpisuję template_id modelu ({template_id}) -> {template_hint}")
            template_id = template_hint
        elif not template_id:
            req_lower = user_request.lower()
            if any(kw in req_lower for kw in ["raport", "sprawozdanie", "analiza"]):
                template_id = "report"
            elif any(kw in req_lower for kw in ["protokół", "protokol", "spotkanie", "zebranie"]):
                template_id = "protocol"
            else:
                template_id = "letter"
            print(f"      ⚠️ [DOC GEN] Model nie wybrał szablonu — domyślnie: {template_id}")

        # Build HITL form
        templates_metadata = _load_templates_metadata()
        hitl_req = build_document_hitl_form(template_id, parameters, templates_metadata)

        print(f"      🛑 [HITL] Przygotowano formularz (szablon={template_id}, {len(parameters)} parametrów)")
        return {
            "messages": [AIMessage(content=f"Przygotowano parametry dokumentu ({template_id}). Oczekuję na potwierdzenie użytkownika...")],
            "execution_status": "pending_hitl",
            "hitl_request": hitl_req,
        }

    except json.JSONDecodeError as e:
        return {
            "messages": [HumanMessage(content=f"BŁĄD: Nieprawidłowy JSON: {e}")],
            "execution_status": "error",
            "hitl_request": None,
            "current_step": None,
        }
    except Exception as e:
        return {
            "messages": [HumanMessage(content=f"BŁĄD: {e}")],
            "execution_status": "error",
            "hitl_request": None,
            "current_step": None,
        }
