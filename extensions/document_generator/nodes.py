"""Document Generator node — PDF documents from LaTeX templates, with a parameter review step (HITL).

1. First pass: the model picks a template and fills its parameters. Under the ``ask`` approval
   policy the user reviews them in a form and the run pauses; under ``auto`` the PDF is generated
   right away.
2. Second pass (after the form): the confirmed parameters are compiled to PDF and the plan goes on.
"""

import json
import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from src.core.tools_common import extract_json_object, invoke_llm_and_normalize
from src.llm_config import get_bielik_chat

PREVIOUS_ANSWER_LIMIT = 6000
JSON_ONLY_RETRY = ('Odpowiedz WYŁĄCZNIE obiektem JSON w formacie {"template_id": "...", "parameters": {...}} — '
                   "bez żadnego tekstu przed ani po nim.")
MAX_JSON_ATTEMPTS = 3  # like the coder: the first answer and two corrections


def _json_problem(raw: str):
    """(parsed object, None) for a usable answer, else (None, what is wrong — told to the model on the retry)."""
    parsed = extract_json_object(raw)
    if parsed is None:
        start = (raw or "").find("{")
        if start < 0:
            return None, "Odpowiedź nie zawierała obiektu JSON."
        try:
            json.loads(raw[start:raw.rfind("}") + 1] or raw[start:])
        except json.JSONDecodeError as exc:
            return None, f"JSON był niepoprawny: {exc.msg} (linia {exc.lineno}, kolumna {exc.colno})."
        return None, "JSON był niepoprawny."
    if not isinstance(parsed.get("template_id"), str) or not isinstance(parsed.get("parameters", {}), dict):
        return None, 'W obiekcie JSON brakowało pola "template_id" (tekst) albo "parameters" nie było obiektem.'
    return parsed, None


def _generate(state, template_id: str, parameters: dict, signature=None) -> dict:
    from src.agent_graph import _advance_plan_step
    from src.agent_tools import tool_generate_pdf_from_template

    result = tool_generate_pdf_from_template(template_id, parameters, signature=signature)
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
    from src.agent_graph import (_contains_no_data_signal, _get_current_turn_ai_messages, _get_previous_turn_answer,
                                 _infer_template_hint, _refers_to_previous_answer, _resolve_effective_user_message)
    from src.agent_tools import (_get_condensed_templates_info, _load_templates_metadata, build_document_hitl_form,
                                 normalize_template_params)

    print("   --> 📄 [NODE: DOCUMENT GENERATOR] Generowanie dokumentu z szablonu...")

    # --- second pass: parameters confirmed in the form
    hitl_response = state.get("hitl_response")
    if hitl_response and hitl_response.get("continuation_type") == "generate_pdf":
        print("      ✅ [HITL] Odebrano potwierdzone parametry — generuję PDF...")
        if not hitl_response.get("template_id"):
            return _error("Brak template_id w odpowiedzi formularza.")
        from src.signatures import mode_from_form

        parameters = hitl_response.get("parameters", {}) or {}
        # the signature comes only from the user's form, never from the model
        return _generate(state, hitl_response["template_id"], parameters,
                         signature=mode_from_form(parameters.get("_signature")))

    # --- first pass: choose a template and fill its parameters
    from src import doc_templates

    user = run_context.current("user")

    def hint(text: str):
        """A user's own/global template named in the request wins over the built-in keyword guess."""
        return doc_templates.custom_hint(text, user) or _infer_template_hint(text)

    messages = state["messages"]
    last_user_msg = _resolve_effective_user_message(messages)
    template_hint = hint(last_user_msg)

    context_parts = [m.content for m in _get_current_turn_ai_messages(messages)
                     if isinstance(m, AIMessage) and m.content and not (template_hint and _contains_no_data_signal(m.content))]
    context = "\n---\n".join(context_parts[-3:])
    if len(context) > 1500:
        context = context[:1500] + "\n[...skrócono kontekst...]"

    # "Wygeneruj ten list jako PDF": the content is the assistant's answer from the previous turn
    previous_answer = _get_previous_turn_answer(messages) if _refers_to_previous_answer(last_user_msg) else ""
    if len(previous_answer) > PREVIOUS_ANSWER_LIMIT:
        previous_answer = previous_answer[:PREVIOUS_ANSWER_LIMIT] + "\n[...skrócono...]"
    if previous_answer:
        print(f"      📎 [DOC GEN] Użytkownik odnosi się do poprzedniej odpowiedzi ({len(previous_answer)} znaków)")

    user_request = last_user_msg
    task_plan = state.get("task_plan") or {}
    current_step = state.get("current_step")
    if task_plan and current_step is not None and 0 <= current_step < len(task_plan.get("steps", [])):
        step_input = task_plan["steps"][current_step].get("input")
        if step_input and last_user_msg:
            user_request = f"Oryginalne zapytanie: {last_user_msg} (Instrukcja do kroku: {step_input})"
            template_hint = hint(last_user_msg) or hint(step_input)
        elif step_input:
            user_request = step_input
            template_hint = hint(step_input)

    llm, system_prompt = get_bielik_chat(role="template_filler")
    forced = (f'WYMUSZENIE SZABLONU: Użytkownik jawnie poprosił o typ dokumentu "{template_hint}". '
              f'Użyj template_id dokładnie "{template_hint}".') if template_hint else ""
    prompt = f"""DOSTĘPNE SZABLONY:
{_get_condensed_templates_info()}

ZAPYTANIE UŻYTKOWNIKA: "{user_request}"

{forced}

{f'POPRZEDNIA ODPOWIEDŹ ASYSTENTA (użytkownik prosi o dokument z tej treści — przenieś ją do parametrów szablonu wiernie, bez wymyślania nowej treści):{chr(10)}{previous_answer}' if previous_answer else ''}

{f'KONTEKST:{chr(10)}{context}' if context else ''}

Wybierz szablon i wypełnij WSZYSTKIE wymagane parametry. Treść po polsku, formalnym językiem.
Jeśli kontekst z RAG zawiera "brak danych", traktuj to jako kontekst pomocniczy i nadal wygeneruj dokument na podstawie zapytania użytkownika.
Dla list użyj \\\\begin{{enumerate}} \\\\item ... \\\\end{{enumerate}}

ZASADY WYBORU: (Priorytet!)
0. Jeśli użytkownik wskazuje szablon z listy po nazwie lub opisie (np. „według szablonu oferty”), użyj tego szablonu
1. Jeśli użytkownik prosi o PODANIE, WNIOSEK, LIST -> "template_id: letter"
2. Jeśli użytkownik prosi o RAPORT, SPRAWOZDANIE, WYNIKI -> "template_id: report"
3. Jeśli użytkownik prosi o PROTOKÓŁ, NOTATKĘ ZE SPOTKANIA -> "template_id: protocol"
Parametry podawaj dokładnie pod nazwami pól wybranego szablonu. Pola listy i tabeli: jedna pozycja / jeden wiersz w linii
(komórki tabeli rozdzielaj znakiem |).

Odpowiedz WYŁĄCZNIE w formacie JSON:
{{
  "template_id": "identyfikator szablonu z listy",
  "parameters": {{
    "PARAM1": "wartość1",
    "PARAM2": "wartość2"
  }}
}}"""

    try:
        conversation = [SystemMessage(content=system_prompt), HumanMessage(content=prompt)]
        raw = invoke_llm_and_normalize(llm, conversation).content
        if raw.startswith("[LLM"):
            return _error(f"Model nie może przetworzyć zapytania.\n{raw[:200]}")
        parsed, problem = _json_problem(raw)
        attempt = 1
        while parsed is None and attempt < MAX_JSON_ATTEMPTS:
            # models sometimes answer with the letter itself, or with JSON broken by an unescaped quote or a
            # stray comma: ask again, saying what was wrong (as the coder gets its error back)
            attempt += 1
            print(f"      ⚠️ [DOC GEN] {problem} Ponawiam (próba {attempt}/{MAX_JSON_ATTEMPTS})")
            conversation = conversation + [AIMessage(content=raw), HumanMessage(
                content=f"{problem} Popraw to (próba {attempt}/{MAX_JSON_ATTEMPTS}). Pamiętaj o cudzysłowach "
                        f'wewnątrz tekstu (\\") i znakach nowej linii (\\n). {JSON_ONLY_RETRY}')]
            raw = invoke_llm_and_normalize(llm, conversation).content
            if raw.startswith("[LLM"):
                return _error(f"Model nie może przetworzyć zapytania.\n{raw[:200]}")
            parsed, problem = _json_problem(raw)
        if parsed is None:
            return _error(f"Model nie zwrócił poprawnego JSON ({MAX_JSON_ATTEMPTS} próby): {problem}\n{raw[:300]}")
    except Exception as exc:
        return _error(str(exc))

    template_id = parsed.get("template_id")
    parameters = parsed.get("parameters", {}) or {}
    chosen = _load_templates_metadata().get("templates", {}).get(template_id or "")
    if template_hint and chosen and chosen.get("kind") == "custom" and template_hint not in doc_templates.custom_ids(user):
        # the model picked the user's own/global template and the request only matched a built-in keyword: keep it
        print(f"      ℹ️ [DOC GEN] Model wybrał szablon własny {template_id} — zostawiam (słowo kluczowe: {template_hint})")
    elif template_hint:
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
