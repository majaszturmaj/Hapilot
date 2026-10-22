"""Document templates (src/doc_templates.py): block builder, checks, private/global templates, built-in edits."""

import pytest

from src import admin, doc_templates as dt

OFFER = {
    "id": "oferta_handlowa", "name": "Oferta handlowa", "description": "Oferta cenowa dla klienta z tabelą pozycji",
    "mode": "blocks",
    "parameters": {
        "KLIENT": {"description": "Nazwa klienta", "example": "Firma Testowa Sp. z o.o.", "required": True},
        "POZYCJE": {"description": "Pozycje oferty", "example": "1|Tłumaczenie|60 zł\n2|Skład|350 zł", "multiline": True},
        "WARUNKI": {"description": "Warunki", "example": "Płatność 14 dni\nWażność 30 dni", "multiline": True},
    },
    "blocks": [
        {"type": "title", "text": "Oferta dla {{KLIENT}}"},
        {"type": "paragraph", "text": "Szanowni Państwo,\n\nprzedstawiamy ofertę — 100% satysfakcji & {{KLIENT}}."},
        {"type": "table", "field": "POZYCJE", "columns": "Lp.;Usługa;Cena"},
        {"type": "heading", "text": "Warunki"},
        {"type": "list", "field": "WARUNKI", "style": "numbers"},
        {"type": "signature", "text": "Anna Przykładowa"},
    ],
}


@pytest.fixture
def tpl(tmp_path, monkeypatch, docker):
    monkeypatch.setenv("HAPILOT_CUSTOM_TEMPLATES_DIR", str(tmp_path / "templates"))
    monkeypatch.delenv("HAPILOT_UI_ADMINS", raising=False)
    yield tmp_path / "templates"
    for path in list(dt.overrides_dir().glob("*")):
        path.unlink()
    if admin.audit_path().exists():
        admin.audit_path().unlink()


# --------------------------------------------------------------------------- builder and checks

def test_blocks_become_latex_with_the_builtin_fonts_and_field_filters():
    latex = dt.render_blocks(OFFER["blocks"])
    assert latex.startswith("\\documentclass") and "\\usepackage{tgtermes}" in latex and latex.rstrip().endswith("\\end{document}")
    assert "Oferta dla {{KLIENT|linie}}" in latex  # address-like blocks keep a value's line breaks
    assert "100\\% satysfakcji \\& {{KLIENT|akapity}}" in latex  # literal text escaped; paragraphs keep paragraphs
    assert "{{POZYCJE|tabela:3}}" in latex and "\\textbf{Usługa}" in latex and "{|X|X|X|}" in latex
    assert "\\begin{enumerate}\n{{WARUNKI|lista}}" in latex and "\\rule{6cm}" in latex
    assert dt.used_fields(latex) == ["KLIENT", "POZYCJE", "WARUNKI"]


def test_multiline_values_keep_lines_and_paragraphs():
    from src.agent_tools import _fill_template

    filled = _fill_template("{{A|linie}}|{{B|akapity}}", {"A": "Firma\nul. 1\n\n00-001", "B": "Raz\ndwa\n\nTrzy & co"})
    assert filled == "Firma \\\\\nul. 1 \\\\\n00-001|Raz \\\\\ndwa\n\nTrzy \\& co"


def test_escape_literal_cannot_inject_latex():
    assert dt.escape_literal("\\input{/etc/passwd} {{A}}") == "\\textbackslash{}input\\{/etc/passwd\\} {{A}}"


@pytest.mark.parametrize("change,message", [
    ({"id": "Zła"}, "Identyfikator"), ({"name": ""}, "Nazwa"), ({"description": ""}, "Opis"),
    ({"parameters": {}}, "co najmniej jedno pole"), ({"blocks": []}, "co najmniej jeden blok"),
    ({"blocks": [{"type": "list", "field": "NIEMA"}]}, "wybierz pole"),
    ({"blocks": [{"type": "paragraph", "text": "{{NIEZNANE}}"}]}, "niezdefiniowanych pól: NIEZNANE"),
    ({"mode": "latex", "latex": "\\begin{document}x\\end{document}"}, "documentclass"),
    ({"parameters": {"zle pole": {"description": "x"}}}, "wielkimi literami")])
def test_drafts_are_checked(change, message):
    with pytest.raises(dt.TemplateError, match=message):
        dt.normalise_draft({**OFFER, **change})


def test_unused_fields_are_reported_not_refused():
    draft = dt.normalise_draft({**OFFER, "parameters": {**OFFER["parameters"], "ZBEDNE": {"description": "x"}}})
    assert dt.check_report(draft)["unused_fields"] == ["ZBEDNE"]


# --------------------------------------------------------------------------- private and global templates

def test_user_template_is_private_compiled_and_offered_only_to_them(tpl, docker):
    created = dt.create_template(OFFER, "uzytkownik")
    assert created["kind"] == "custom" and created["mine"] and not created["global"] and created["editable"]
    assert docker.runs and "pdflatex" in docker.runs[-1]["command"][2]  # compiled before it was saved
    assert (tpl / "uzytkownik" / "oferta_handlowa" / "template.tex").is_file()
    assert "oferta_handlowa" in dt.catalog("uzytkownik")["templates"]
    assert "oferta_handlowa" not in dt.catalog("anna")["templates"] and "oferta_handlowa" not in dt.catalog("admin")["templates"]
    assert set(dt.catalog(None)["templates"]) == {"letter", "report", "protocol"}  # evaluation: repository only
    assert dt.custom_hint("Przygotuj ofertę handlową według szablonu Oferta handlowa", "uzytkownik") == "oferta_handlowa"
    assert dt.custom_hint("Przygotuj ofertę handlową według szablonu Oferta handlowa", "anna") is None
    assert [t["id"] for t in dt.list_templates("anna")] == ["letter", "report", "protocol"]


def test_template_that_does_not_compile_is_not_saved(tpl, docker):
    docker.latex_fails = True
    with pytest.raises(dt.TemplateError, match="nie kompiluje"):
        dt.create_template(OFFER, "uzytkownik")
    assert not list(tpl.glob("*/*/template.json")) if tpl.exists() else True
    result = dt.preview(OFFER, "uzytkownik")
    assert result["ok"] is False and "BŁĄD KOMPILACJI" in result["log"]


def test_preview_compiles_with_example_values_and_cleans_up(tpl, docker):
    from src.config import get_settings

    result = dt.preview(OFFER, "uzytkownik")
    assert result["ok"] and result["unused_fields"] == []
    tex = docker.runs[-1]["command"][2]
    assert "podglad.tex" in tex
    assert not list(get_settings().workspace_root.glob("tplpreview_uzytkownik_*"))  # temporary workspace removed


def test_only_admins_share_and_global_templates_are_read_only_for_users(tpl):
    with pytest.raises(dt.TemplateError, match="Tylko administrator"):
        dt.create_template({**OFFER, "global": True}, "uzytkownik")
    dt.create_template(OFFER, "admin")
    assert "oferta_handlowa" not in dt.catalog("uzytkownik")["templates"]
    dt.set_global("oferta_handlowa", True, "admin")
    seen = dt.get_template("oferta_handlowa", "uzytkownik")
    assert seen["global"] and not seen["editable"] and "oferta_handlowa" in dt.catalog("anna")["templates"]
    with pytest.raises(dt.TemplateError, match="administrator"):
        dt.update_template("oferta_handlowa", OFFER, "uzytkownik")
    with pytest.raises(dt.TemplateError, match="administrator"):
        dt.delete_template("oferta_handlowa", "uzytkownik")
    with pytest.raises(dt.TemplateError, match="Tylko administrator"):
        dt.set_global("oferta_handlowa", False, "uzytkownik")
    dt.set_global("oferta_handlowa", False, "admin")
    assert "oferta_handlowa" not in dt.catalog("anna")["templates"]
    assert [e["action"] for e in admin.audit_log("admin")][:3] == ["template_unshared", "template_shared", "template_created"]


def test_owner_edits_switches_to_latex_and_deletes(tpl):
    dt.create_template(OFFER, "uzytkownik")
    latex = dt.render_blocks(OFFER["blocks"]).replace("Oferta dla {{KLIENT|linie}}", "Propozycja dla {{KLIENT}}")
    updated = dt.update_template("oferta_handlowa", {**OFFER, "mode": "latex", "latex": latex}, "uzytkownik")
    assert updated["mode"] == "latex" and "Propozycja dla {{KLIENT}}" in updated["latex"]
    with pytest.raises(dt.TemplateError, match="Nie ma takiego"):
        dt.update_template("oferta_handlowa", OFFER, "anna")
    with pytest.raises(dt.TemplateError, match="zajęty"):
        dt.create_template(OFFER, "anna")  # ids are unique across users
    dt.delete_template("oferta_handlowa", "uzytkownik")
    assert "oferta_handlowa" not in dt.catalog("uzytkownik")["templates"]
    with pytest.raises(dt.TemplateError, match="nie można usunąć"):
        dt.delete_template("letter", "admin")


# --------------------------------------------------------------------------- built-in templates

def test_admin_edits_builtin_with_history_users_cannot(tpl):
    original = dt.get_template("letter", "admin")
    edit = {"name": "List firmowy", "description": original["description"], "parameters": original["parameters"],
            "latex": original["latex"].replace("\\linespread{1.3}", "\\linespread{1.2}")}
    with pytest.raises(dt.TemplateError, match="tylko administrator"):
        dt.update_template("letter", edit, "uzytkownik")
    saved = dt.update_template("letter", edit, "admin")
    assert saved["overridden"] and saved["name"] == "List firmowy" and "\\linespread{1.2}" in saved["latex"]
    assert dt.catalog("anna")["templates"]["letter"]["name"] == "List firmowy"  # every user's chats use the edit
    assert dt.catalog(None)["templates"]["letter"]["name"] == original["name"]  # evaluation does not
    smaller = {**edit, "parameters": {k: v for k, v in edit["parameters"].items() if k != "BODY"},
               "latex": edit["latex"].replace("{{BODY}}", "")}
    with pytest.raises(dt.TemplateError, match="zachować swoje pola: BODY"):
        dt.update_template("letter", smaller, "admin")
    restored = dt.restore_builtin("letter", "admin")
    assert not restored["overridden"] and restored["name"] == original["name"]
    assert [h["restored"] for h in restored["history"]] == [True, False]


# --------------------------------------------------------------------------- documents from a custom template

def test_document_node_uses_the_users_template(tpl, docker, llm):
    from langchain_core.messages import HumanMessage

    from src import run_context
    from src.agent_tools import _params_path

    import extensions.document_generator.nodes as dg

    dt.create_template(OFFER, "uzytkownik")
    llm.on("template_filler", '{"template_id": "letter", "parameters": {"KLIENT": "Firma Testowa", '
                              '"POZYCJE": "1|Tłumaczenie|60 zł", "WARUNKI": "- Płatność 14 dni\\n- Ważność 30 dni"}}')
    state = {"messages": [HumanMessage(content="Przygotuj ofertę dla Firmy Testowej według szablonu Oferta handlowa")],
             "task_plan": {}, "current_step": None, "hitl_response": None}
    with run_context.use_context(session_id="s-offer", user="uzytkownik", approval_policy="auto"):
        out = dg.run_document_generator(state)
        assert out["execution_status"] == "success", out["messages"][-1].content
        name = out["last_generated_file"]
        tex = run_context.workspace_file(name).read_text(encoding="utf-8")
        assert _params_path(name).is_file()
    assert name.startswith("oferta_handlowa_")  # the request named the template, the built-in keyword lost
    assert "Oferta dla Firma Testowa" in tex and "1 & Tłumaczenie & 60 zł \\\\ \\hline" in tex
    assert "\\item Płatność 14 dni" in tex and "\\item Ważność 30 dni" in tex
    assert "oferta_handlowa" in llm.prompts_for("template_filler")[0]  # the model saw the template in its list
