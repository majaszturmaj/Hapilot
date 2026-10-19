"""The knowledge node inside a plan: answers from the documents, and what happens when they have no answer."""

import json

import pytest

from src import knowledge_base

CHUNK = ("kadry/regulamin_urlopow.txt", "Wniosek urlopowy składa się co najmniej 14 dni kalendarzowych przed urlopem.")


class OneChunkBackend:
    def dense(self, path, collection, query, k):
        return [("c1", CHUNK[1], {"source": CHUNK[0], "chunk_id": "c1"}, 0.9)]

    def all_chunks(self, path, collection):
        return [("c1", CHUNK[1], {"source": CHUNK[0], "chunk_id": "c1"})]


@pytest.fixture(autouse=True)
def backend():
    knowledge_base.set_backend(OneChunkBackend())
    yield
    knowledge_base.set_backend(None)


def plan(*steps, complexity="medium"):
    return json.dumps({"task_name": "t", "complexity": complexity,
                       "steps": [{"id": i + 1, "action": a, "tool": t, "input": a} for i, (t, a) in enumerate(steps)]})


def test_answer_uses_found_documents(graph, invoke, llm):
    llm.on("task_planner", plan(("rag_search", "Termin wniosku urlopowego"), complexity="low"))
    llm.on("knowledge", "wniosek urlopowy termin", "Wniosek składa się 14 dni przed urlopem [kadry/regulamin_urlopow.txt].")
    state = invoke(graph, "Ile dni przed urlopem trzeba złożyć wniosek?", "kn-ok", approval_policy="auto")
    assert "14 dni" in state["messages"][-1].content
    synthesis_prompt = llm.prompts_for("knowledge")[1]
    assert "--- ŹRÓDŁO: kadry/regulamin_urlopow.txt ---" in synthesis_prompt and "wniosek urlopowy termin" in synthesis_prompt


def test_short_no_data_answer_stops_a_multi_step_plan(graph, invoke, llm):
    llm.on("task_planner", plan(("rag_search", "Limit noclegu"), ("llm_synthesis", "Napisz maila z limitem")))
    llm.on("knowledge", "limit nocleg delegacja", "Nie znaleziono informacji o limicie noclegu w dokumentach.")
    state = invoke(graph, "Sprawdź limit noclegu i napisz maila do zespołu", "kn-abort", approval_policy="auto")
    assert state["messages"][-1].content.startswith("Nie znaleziono informacji")
    assert not any(role == "writer" for role, _ in llm.calls)  # the e-mail is not written from nothing


def test_long_answer_with_a_no_data_phrase_continues_the_plan(graph, invoke, llm):
    long_answer = "Dokumenty nie zawierają limitu dla delegacji zagranicznych. " + "Regulamin opisuje urlopy szczegółowo. " * 10
    llm.on("task_planner", plan(("rag_search", "Zasady"), ("llm_synthesis", "Podsumuj")))
    llm.on("knowledge", "zasady", long_answer)
    llm.on("writer", "podsumowanie")
    state = invoke(graph, "Sprawdź zasady i podsumuj", "kn-long", approval_policy="auto")
    assert state["messages"][-1].content == "podsumowanie"


def test_no_data_before_a_document_step_does_not_block_the_document(graph, invoke, llm, docker):
    from test_graph_flows import LETTER

    llm.on("task_planner", plan(("rag_search", "Zasady przeglądów"), ("template_pdf", "Pismo o przegląd drukarek")))
    llm.on("knowledge", "przegląd drukarek", "Brak informacji o przeglądach w dokumentach.")
    llm.on("template_filler", LETTER)
    state = invoke(graph, "Sprawdź zasady przeglądów i przygotuj pismo w PDF o przegląd drukarek", "kn-pdf",
                   approval_policy="auto")
    assert "Wygenerowano plik: letter_" in state["messages"][-1].content
    filler_prompt = llm.prompts_for("template_filler")[0]
    assert "To NIE blokuje zadania" in filler_prompt
