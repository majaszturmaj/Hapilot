"""End-to-end graph runs with a scripted model and fake Docker (regression tests for Stage 1 bugs)."""

import json

from langchain_core.messages import HumanMessage

from src import run_context


def plan(*steps, complexity="low"):
    return json.dumps({"task_name": "t", "complexity": complexity,
                       "steps": [{"id": i + 1, "action": a, "tool": t, "input": inp} for i, (t, a, inp) in enumerate(steps)]})


def last_text(state):
    return state["messages"][-1].content


# --------------------------------------------------------------------------- bug: plan-step Python ran the step text

def test_python_step_runs_generated_code_not_step_text(graph, invoke, llm, docker):
    llm.on("task_planner", plan(("python", "Oblicz sumę kwadratów", "Oblicz sumę kwadratów liczb od 1 do 100")))
    llm.on("coder", "Oto kod:\n```python\nprint(sum(i*i for i in range(1, 101)))\n```")
    state = invoke(graph, "Napisz program, który obliczy sumę kwadratów liczb od 1 do 100.", "s-python")
    assert "SUKCES" in last_text(state) and "338350" in last_text(state)
    assert state["generated_code"].startswith("print(sum(")
    assert len(docker.runs) == 1  # no retries


def test_python_error_is_retried_then_fixed(graph, invoke, llm, docker):
    llm.on("task_planner", plan(("python", "Dzielenie", "Podziel 10 przez 2")))
    llm.on("coder", "```python\nprint(10/0)\n```", "```python\nprint(10/2)\n```")
    state = invoke(graph, "Podziel 10 przez 2 w Pythonie", "s-retry")
    assert "5.0" in last_text(state)
    assert len(docker.runs) == 2
    retry_prompt = llm.prompts_for("coder")[1]
    assert "ZeroDivisionError" in retry_prompt and "print(10/0)" in retry_prompt


# --------------------------------------------------------------------------- bug: >2-step plans always waited for approval

THREE_STEPS = plan(("llm_synthesis", "Szkic", "Szkic"), ("llm_synthesis", "Popraw", "Popraw"),
                   ("chat", "Podsumuj", "Podsumuj"), complexity="medium")


def test_long_plan_asks_for_approval_under_ask_policy(graph, invoke, llm):
    llm.on("task_planner", THREE_STEPS)
    state = invoke(graph, "Zrób szkic, popraw go i podsumuj", "s-ask", approval_policy="ask")
    assert state["hitl_request"]["continuation"]["type"] == "approve_plan"
    assert not any(r == "writer" for r, _ in llm.calls)


def test_long_plan_runs_through_under_auto_policy(graph, invoke, llm):
    llm.on("task_planner", THREE_STEPS)
    llm.on("writer", "szkic", "poprawiony szkic")
    llm.on("chat", "podsumowanie")
    state = invoke(graph, "Zrób szkic, popraw go i podsumuj", "s-auto", approval_policy="auto")
    assert not state.get("hitl_request")
    assert last_text(state) == "podsumowanie"
    assert [r for r, _ in llm.calls] == ["task_planner", "writer", "writer", "chat"]


# --------------------------------------------------------------------------- bug: plan lost after a confirmation step

def test_plan_continues_after_confirmation(graph, invoke, llm):
    llm.on("task_planner", plan(("llm_synthesis", "Szkic maila", "Szkic"), ("hitl_confirm", "Czy wysłać dalej?", ""),
                                ("chat", "Zakończ", "Zakończ")))
    llm.on("writer", "szkic maila")
    llm.on("chat", "gotowe po potwierdzeniu")
    approval = invoke(graph, "Napisz szkic i zapytaj mnie przed dalszym krokiem", "s-confirm")
    plan_request = approval["hitl_request"]
    assert plan_request["continuation"]["type"] == "approve_plan"
    approved = {**plan_request["continuation"], "continuation_type": "approve_plan", "confirmed": True}
    first = invoke(graph, approval["messages"], "s-confirm", hitl_response=approved)
    request = first["hitl_request"]
    assert request["continuation"]["type"] == "continue_plan"
    assert first["messages"][-2].content == "szkic maila"
    assert request["continuation"]["saved_plan"]["steps"][2]["tool"] == "chat"

    response = {**request["continuation"], "continuation_type": "continue_plan", "confirmed": True}
    second = invoke(graph, first["messages"], "s-confirm", hitl_response=response)
    assert last_text(second) == "gotowe po potwierdzeniu"


def test_rejected_confirmation_stops_plan(graph, invoke, llm):
    llm.on("task_planner", plan(("hitl_confirm", "Kontynuować?", ""), ("chat", "x", "x")))
    first = invoke(graph, "Zapytaj mnie zanim zaczniesz", "s-reject")
    response = {**first["hitl_request"]["continuation"], "continuation_type": "continue_plan", "confirmed": False}
    second = invoke(graph, first["messages"], "s-reject", hitl_response=response)
    assert "przerwałem" in last_text(second)
    assert not any(r == "chat" for r, _ in llm.calls)


# --------------------------------------------------------------------------- guards in the planner

def test_unrequested_pdf_step_is_removed(graph, invoke, llm):
    llm.on("task_planner", plan(("llm_synthesis", "Mail", "Mail"), ("template_pdf", "PDF", "PDF")))
    llm.on("writer", "Treść maila")
    state = invoke(graph, "Napisz krótkiego maila do zespołu. Nie rób z tego PDF, tylko napisz w oknie czatu.", "s-guard")
    assert last_text(state) == "Treść maila"
    assert not any(r == "template_filler" for r, _ in llm.calls)


def test_missing_pdf_step_is_added_for_an_explicit_request(graph, invoke, llm, docker):
    llm.on("task_planner", plan(("llm_synthesis", "Treść", "Treść pisma")))
    llm.on("writer", "Treść pisma")
    llm.on("template_filler", LETTER)
    state = invoke(graph, "Przygotuj pismo w PDF do działu kadr z prośbą o urlop", "s-add-pdf", approval_policy="auto")
    assert "Wygenerowano plik: letter_" in last_text(state)
    assert [s["tool"] for s in state["task_plan"]["steps"]] == ["llm_synthesis", "template_pdf"]


def test_style_request_without_document_is_not_forced_to_modify(graph, invoke, llm):
    llm.on("task_planner", plan(("llm_synthesis", "Uprość", "Napisz to prościej")))
    llm.on("writer", "Prostsza wersja")
    state = invoke(graph, "Napisz to prościej i krócej, po żołniersku.", "s-style")
    assert last_text(state) == "Prostsza wersja"


def test_planner_failure_falls_back_to_chat(graph, invoke, llm):
    llm.on("task_planner", "to nie jest JSON")
    llm.on("chat", "Cześć!")
    state = invoke(graph, "Hej", "s-noplan")
    assert last_text(state) == "Cześć!"


# --------------------------------------------------------------------------- documents and approval policy

LETTER = json.dumps({"template_id": "letter", "parameters": {
    "SENDER_NAME": "Jan Kowalski", "CITY_DATE": "Wrocław, 5 marca 2026 r.", "SENDER_COMPANY": "Przykładowa Sp. z o.o.",
    "SENDER_ADDRESS": "ul. Zmyślona 7, 50-001 Wrocław", "RECIPIENT_TITLE": "Pani", "RECIPIENT_NAME": "Anna Nowak",
    "RECIPIENT_COMPANY": "Dział Kadr", "RECIPIENT_ADDRESS": "ul. Zmyślona 7, 50-001 Wrocław",
    "DOCUMENT_TITLE": "Wniosek o urlop", "GREETING": "Szanowna Pani,", "BODY": "Proszę o urlop.", "CLOSING": "Z poważaniem"}})


def test_document_form_under_ask_policy_keeps_plan(graph, invoke, llm, docker):
    llm.on("task_planner", plan(("template_pdf", "Podanie", "Podanie o urlop")))
    llm.on("template_filler", LETTER)
    first = invoke(graph, "Napisz podanie o urlop", "s-form", approval_policy="ask")
    req = first["hitl_request"]
    assert req["type"] == "form" and docker.runs == []
    assert req["continuation"]["saved_plan"]["steps"][0]["tool"] == "template_pdf"

    response = {**req["continuation"], "continuation_type": "generate_pdf"}
    second = invoke(graph, first["messages"], "s-form", hitl_response=response)
    assert "Wygenerowano plik: letter_" in last_text(second)
    assert second["last_generated_file"].endswith(".tex")


def test_document_generated_directly_under_auto_policy(graph, invoke, llm, docker):
    llm.on("task_planner", plan(("template_pdf", "Podanie", "Podanie o urlop")))
    llm.on("template_filler", LETTER)
    state = invoke(graph, "Napisz podanie o urlop", "s-auto-doc", approval_policy="auto")
    assert not state.get("hitl_request")
    assert "Wygenerowano plik: letter_" in last_text(state)
    with run_context.use_context(session_id="s-auto-doc"):
        pdf = run_context.workspace_file(last_text(state).split("Wygenerowano plik: ")[1].split()[0])
    assert pdf.is_file()


def test_edit_request_without_document_is_not_forced(graph, invoke, llm):
    llm.on("task_planner", plan(("chat", "Odpowiedz", "Zmień datę")))
    llm.on("chat", "Nie mam jeszcze dokumentu do zmiany.")
    state = invoke(graph, "Zmień datę na 1 czerwca", "s-nodoc")
    assert last_text(state) == "Nie mam jeszcze dokumentu do zmiany."


def test_messages_from_ui_history_are_respected(graph, invoke, llm):
    llm.on("task_planner", plan(("chat", "Odpowiedz", "")))
    llm.on("chat", lambda msgs: "widzę: " + " | ".join(m.content for m in msgs if isinstance(m, HumanMessage)))
    state = invoke(graph, [HumanMessage(content="pierwsza"), HumanMessage(content="druga")], "s-hist")
    assert "pierwsza" in last_text(state) and "druga" in last_text(state)  # merged into one user turn


# --------------------------------------------------------------------------- bug found on the GPU host (llama.cpp 400)

def _strict_template(messages):
    """What llama.cpp's chat templates enforce: roles alternate after the system prompt; the list ends with a user turn."""
    from langchain_core.messages import AIMessage, SystemMessage

    roles = ["ai" if isinstance(m, AIMessage) else "human" for m in messages if not isinstance(m, SystemMessage)]
    assert roles and roles[-1] == "human", f"conversation must end with a user turn: {roles}"
    assert all(a != b for a, b in zip(roles, roles[1:])), f"roles must alternate: {roles}"
    return "odpowiedź czatu"


def test_chat_after_other_steps_sends_a_valid_conversation(graph, invoke, llm):
    llm.on("task_planner", plan(("llm_synthesis", "Szkic", "Szkic"), ("llm_synthesis", "Popraw", "Popraw"),
                                ("chat", "Odpowiedz krótko", "Odpowiedz krótko")))
    llm.on("writer", "szkic", "poprawiony szkic")
    llm.on("chat", _strict_template)
    state = invoke(graph, "Napisz szkic, popraw i odpowiedz", "s-strict", approval_policy="auto")
    assert last_text(state) == "odpowiedź czatu"
    chat_prompt = llm.prompts_for("chat")[0]
    assert "poprawiony szkic" in chat_prompt and "Odpowiedz krótko" in chat_prompt


def test_coder_sees_earlier_step_results_and_readable_files(graph, invoke, llm, docker):
    """Regression (found on the GPU host): the coder did not get what the knowledge search found."""
    llm.on("task_planner", plan(("llm_synthesis", "Znajdź cenę", "Podaj cenę składu raportu"),
                                ("python", "Policz brutto", "Policz cenę brutto (VAT 23%)")))
    llm.on("writer", "Cena składu raportu: 350 zł netto.")
    llm.on("coder", "```python\nprint(round(350 * 1.23, 2))\n```")
    state = invoke(graph, "Ile kosztuje skład raportu brutto?", "s-ctx", approval_policy="auto")
    assert "430.5" in last_text(state)
    coder_prompt = llm.prompts_for("coder")[0]
    assert "DANE Z POPRZEDNICH KROKÓW" in coder_prompt and "350 zł netto" in coder_prompt
    assert "/data/" in coder_prompt  # the demo company documents are readable by code
    assert docker.runs[-1]["volumes"]  # and mounted read-only
    assert any(v == {"bind": "/data", "mode": "ro"} for v in docker.runs[-1]["volumes"].values())


def test_planner_json_with_trailing_brace_is_parsed(graph, invoke, llm):
    """Regression (Bielik on the GPU host): an extra '}' after the plan made the greedy regex fail."""
    llm.on("task_planner", plan(("chat", "Odpowiedz", "")) + "}")
    llm.on("chat", "odpowiedź z planu")
    state = invoke(graph, "Hej", "s-brace")
    assert state["task_plan"]["steps"][0]["tool"] == "chat" and last_text(state) == "odpowiedź z planu"


def test_extract_json_object_variants():
    from src.core.tools_common import extract_json_object

    assert extract_json_object('{"a": 1}}') == {"a": 1}
    assert extract_json_object('Oto plan:\n```json\n{"a": {"b": [1, 2]}}\n```') == {"a": {"b": [1, 2]}}
    assert extract_json_object('{zły} potem {"ok": true}') == {"ok": True}
    assert extract_json_object("brak") is None


# --------------------------------------------------------------------------- editing a generated document through the graph

def _generate_letter(graph, invoke, llm, sid):
    llm.on("task_planner", plan(("template_pdf", "Podanie", "Podanie o urlop")))
    llm.on("template_filler", LETTER)
    state = invoke(graph, "Napisz podanie o urlop", sid, approval_policy="auto")
    assert state["last_generated_file"].endswith(".tex")
    return state


def test_edit_of_generated_document_produces_a_new_pdf(graph, invoke, llm, docker):
    from langchain_core.messages import HumanMessage as Human

    first = _generate_letter(graph, invoke, llm, "s-edit")
    llm.replies.clear()
    llm.on("task_planner", plan(("chat", "Odpowiedz", "Zmień datę")))  # the guard turns this into modify_document
    llm.on("template_filler", json.dumps({"CITY_DATE": "Wrocław, 1 czerwca 2026 r."}))
    second = invoke(graph, first["messages"] + [Human(content="Zmień datę w tym podaniu na 1 czerwca 2026")], "s-edit",
                    approval_policy="auto", last_file=first["last_generated_file"])
    assert [s["tool"] for s in second["task_plan"]["steps"]] == ["modify_document"]
    assert "Wygenerowano plik: letter_" in last_text(second)
    assert second["last_generated_file"] != first["last_generated_file"]
    with run_context.use_context(session_id="s-edit"):
        assert "1 czerwca 2026" in run_context.workspace_file(second["last_generated_file"]).read_text(encoding="utf-8")


def test_failed_edit_reports_the_error_and_keeps_the_document(graph, invoke, llm, docker):
    from langchain_core.messages import HumanMessage as Human

    first = _generate_letter(graph, invoke, llm, "s-edit-bad")
    llm.replies.clear()
    llm.on("task_planner", plan(("modify_document", "Zmień", "Zmień datę")))
    llm.on("template_filler", "to nie jest JSON")
    llm.on("chat", lambda msgs: "Nie udało się: " + msgs[-1].content[:80])
    second = invoke(graph, first["messages"] + [Human(content="Zmień datę w tym podaniu na 1 czerwca 2026")], "s-edit-bad",
                    approval_policy="auto", last_file=first["last_generated_file"])
    assert "BŁĄD" in "\n".join(m.content for m in second["messages"][len(first["messages"]):])
    assert second["last_generated_file"] == first["last_generated_file"]
    assert len(docker.runs) == 1  # nothing recompiled


def test_trailing_confirmation_is_dropped_so_the_document_stays_the_answer(graph, invoke, llm, docker):
    # Bielik planned this for "Przygotuj podanie o urlop…" (integration run 2026-10-19): the PDF was made, but the
    # final answer was the confirmation's "(Potwierdzono automatycznie.)", so the UI showed no download.
    llm.on("task_planner", plan(("template_pdf", "Podanie", "Podanie o urlop"), ("hitl_confirm", "Czy wysłać?", "")))
    llm.on("template_filler", LETTER)
    state = invoke(graph, "Przygotuj podanie o urlop", "s-trailing", approval_policy="auto")
    assert [s["tool"] for s in state["task_plan"]["steps"]] == ["template_pdf"]
    assert "Wygenerowano plik: letter_" in last_text(state)


def test_confirmation_between_steps_is_kept(graph, invoke, llm):
    llm.on("task_planner", plan(("llm_synthesis", "Szkic", "Szkic"), ("hitl_confirm", "Dalej?", ""), ("chat", "Koniec", "")))
    llm.on("writer", "szkic")
    llm.on("chat", "koniec")
    state = invoke(graph, "Szkic, potwierdzenie, koniec", "s-middle", approval_policy="auto")
    assert [s["tool"] for s in state["task_plan"]["steps"]] == ["llm_synthesis", "hitl_confirm", "chat"]
