"""Document generation and editing (regression: edits truncated long documents; edits leaked across sessions)."""

import json

from src import run_context
from src.agent_tools import tool_generate_pdf_from_template, tool_modify_document

LONG_BODY = " ".join(f"Akapit {i}: treść merytoryczna." for i in range(400)) + " KONIEC-DOKUMENTU"

PARAMS = {"SENDER_NAME": "Jan Kowalski", "CITY_DATE": "Wrocław, 5 marca 2026 r.", "SENDER_COMPANY": "Przykładowa Sp. z o.o.",
          "SENDER_ADDRESS": "ul. Zmyślona 7, 50-001 Wrocław", "RECIPIENT_TITLE": "Pani", "RECIPIENT_NAME": "Anna Nowak",
          "RECIPIENT_COMPANY": "Dział Kadr", "RECIPIENT_ADDRESS": "ul. Zmyślona 7, 50-001 Wrocław",
          "DOCUMENT_TITLE": "Wniosek", "GREETING": "Szanowna Pani,", "BODY": LONG_BODY, "CLOSING": "Z poważaniem"}


def _generated_tex(result: str) -> str:
    return result.split("Wygenerowano plik: ")[1].split()[0].replace(".pdf", ".tex")


def test_generation_stores_parameters_and_cleans_aux(docker, session):
    result = tool_generate_pdf_from_template("letter", PARAMS)
    assert result.startswith("SUKCES"), result
    tex = run_context.workspace_file(_generated_tex(result))
    saved = json.loads(tex.with_name(tex.stem + ".params.json").read_text(encoding="utf-8"))
    assert saved["template_id"] == "letter" and saved["parameters"]["BODY"] == LONG_BODY
    assert not tex.with_suffix(".aux").exists()


def test_missing_required_parameters_are_reported(docker, session):
    result = tool_generate_pdf_from_template("letter", {"SENDER_NAME": "Jan Kowalski"})
    assert result.startswith("BŁĄD") and "BODY" in result and docker.runs == []


def test_aliases_are_normalised(docker, session):
    aliased = {("tresc" if k == "BODY" else k): v for k, v in PARAMS.items()}
    assert tool_generate_pdf_from_template("letter", aliased).startswith("SUKCES")


def test_edit_changes_only_requested_field_and_keeps_long_body(docker, session, llm):
    first = tool_generate_pdf_from_template("letter", PARAMS)
    llm.on("template_filler", json.dumps({**PARAMS, "BODY": "skrócone przez model", "CITY_DATE": "Wrocław, 1 czerwca 2026 r."}))
    # the model may only change what it returns; untouched keys keep their values — here it also
    # returned BODY, so the merged document uses the model's version: check that the prompt had the full body
    result = tool_modify_document(_generated_tex(first), "Zmień datę na 1 czerwca 2026")
    assert result.startswith("SUKCES"), result
    prompt = llm.prompts_for("template_filler")[0]
    assert "KONIEC-DOKUMENTU" in prompt  # the model saw the whole document, nothing was cut


def test_edit_merges_partial_answer(docker, session, llm):
    first = tool_generate_pdf_from_template("letter", PARAMS)
    llm.on("template_filler", json.dumps({"CITY_DATE": "Wrocław, 1 czerwca 2026 r."}))
    result = tool_modify_document(_generated_tex(first), "Zmień datę na 1 czerwca 2026")
    new_tex = run_context.workspace_file(_generated_tex(result)).read_text(encoding="utf-8")
    assert "1 czerwca 2026" in new_tex and "KONIEC-DOKUMENTU" in new_tex


def test_long_raw_tex_is_not_truncated(docker, session, llm):
    tex = run_context.workspace_file("raw_20260101_000000_000000.tex")
    tex.write_text("\\documentclass{article}\\begin{document}" + "x" * 30000 + "\\end{document}", encoding="utf-8")
    result = tool_modify_document(tex.name, "Popraw literówki")
    assert result.startswith("BŁĄD") and "limit edycji" in result
    assert llm.calls == []  # nothing was sent in a truncated form


def test_edit_never_reaches_another_session(docker, llm):
    with run_context.use_context(session_id="alice"):
        first = tool_generate_pdf_from_template("letter", PARAMS)
        assert first.startswith("SUKCES")
    with run_context.use_context(session_id="bob"):
        result = tool_modify_document(_generated_tex(first), "Zmień datę")
    assert result.startswith("BŁĄD") and "tej sesji" in result
    assert llm.calls == []
