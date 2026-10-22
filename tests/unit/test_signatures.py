"""Signatures in documents (src/signatures.py): scans per user, the form choice, the signature place in templates."""

import struct

import pytest

from src import signatures
from src.source_view import png


def tiny_png(w=120, h=40):
    return png(bytes([255, 255, 255]) * w * h, w, h, w * 3, 3)


def tiny_jpeg(w=300, h=90):
    """SOI, an APP0 segment, then SOF0 with the size — enough for the header check."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


@pytest.fixture
def scans():
    yield
    for user in ("uzytkownik", "anna", "admin"):
        signatures.delete(user)


def test_png_and_jpeg_are_recognised_by_content():
    assert signatures.image_info(tiny_png()) == ("png", 120, 40)
    assert signatures.image_info(tiny_jpeg()) == ("jpg", 300, 90)
    for bad in (b"GIF89a....", b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"%PDF-1.4", b""):
        with pytest.raises(signatures.SignatureError):
            signatures.check(bad)
    with pytest.raises(signatures.SignatureError, match="za duży"):
        signatures.check(tiny_png() + b"\x00" * signatures.MAX_BYTES)
    with pytest.raises(signatures.SignatureError, match="rozmiar"):
        signatures.check(tiny_png(5, 5))


def test_scans_are_per_user(scans):
    saved = signatures.save("uzytkownik", tiny_png())
    assert saved["present"] and saved["src"].startswith("data:image/png;base64,") and saved["width"] == 120
    assert signatures.path("uzytkownik").name == "signature.png"
    assert signatures.path("anna") is None and signatures.path("admin") is None and signatures.path(None) is None
    signatures.save("uzytkownik", tiny_jpeg())  # a new scan replaces the old one
    assert signatures.path("uzytkownik").name == "signature.jpg"
    assert signatures.delete("uzytkownik") == {"present": False} and signatures.path("uzytkownik") is None


def test_form_field_offers_the_scan_only_to_its_owner(scans):
    latex = "a {{PODPIS_SLOT}} b"
    assert signatures.form_field("uzytkownik", "no slot here") is None
    field = signatures.form_field("uzytkownik", latex)
    assert field["options"] == [signatures.BLANK_LABEL] and field["value"] == signatures.BLANK_LABEL
    signatures.save("uzytkownik", tiny_png())
    field = signatures.form_field("uzytkownik", latex)
    assert field["options"] == [signatures.BLANK_LABEL, signatures.SCAN_LABEL]
    assert field["value"] == signatures.BLANK_LABEL  # never pre-selected
    assert signatures.form_field("uzytkownik", latex, chosen="scan")["value"] == signatures.SCAN_LABEL  # earlier choice
    assert signatures.form_field("anna", latex)["options"] == [signatures.BLANK_LABEL]
    assert signatures.mode_from_form(signatures.SCAN_LABEL) == "scan" and signatures.mode_from_form("cokolwiek") == "blank"


def test_slot_is_a_fixed_box_blank_or_with_the_scan(scans, tmp_path):
    doc = "\\documentclass{article}\n\\begin{document}\n{{PODPIS_SLOT}}\n\\end{document}"
    blank, mode = signatures.fill_slot(doc, "scan", "uzytkownik", tmp_path)  # no scan uploaded: blank
    assert mode == "blank" and "\\parbox[b][1.5cm][b]{6cm}" in blank and "includegraphics" not in blank
    signatures.save("uzytkownik", tiny_png())
    filled, mode = signatures.fill_slot(doc, "scan", "uzytkownik", tmp_path)
    copied = [p.name for p in tmp_path.iterdir()]
    assert mode == "scan" and len(copied) == 1 and copied[0].startswith("podpis_") and copied[0].endswith(".png")
    assert f"\\includegraphics[width=5.6cm,height=1.45cm,keepaspectratio]{{{copied[0]}}}" in filled
    assert "\\usepackage{graphicx}\n\\begin{document}" in filled  # added when the template lacks it
    assert signatures.fill_slot(doc, "blank", "uzytkownik", tmp_path)[1] == "blank"
    assert signatures.fill_slot("no slot", "scan", "uzytkownik", tmp_path) == ("no slot", "none")
    with_graphicx = doc.replace("\\begin{document}", "\\usepackage[final]{graphicx}\n\\begin{document}")
    assert signatures.fill_slot(with_graphicx, "scan", "uzytkownik", tmp_path)[0].count("graphicx") == 1


def test_builtin_templates_and_builder_have_the_signature_place():
    from src import doc_templates as dt
    from src.agent_tools import TEMPLATES_DIR

    for name in ("letter", "report", "protocol"):
        assert (TEMPLATES_DIR / f"{name}.tex").read_text(encoding="utf-8").count("{{PODPIS_SLOT}}") == 1, name
    assert "{{PODPIS_SLOT}}" in dt.render_block({"type": "signature", "text": "Anna"})
    assert dt.used_fields("{{A}} {{PODPIS_SLOT}}") == ["A"]  # reserved: not a field
    with pytest.raises(dt.TemplateError, match="miejsce na podpis"):
        dt.normalise_draft({"id": "abc", "name": "x", "description": "y", "mode": "latex",
                            "latex": "\\documentclass{article}\\begin{document}\\end{document}",
                            "parameters": {"PODPIS_SLOT": {"description": "x"}}})


LETTER = {"SENDER_NAME": "Anna Przykładowa", "CITY_DATE": "Wrocław, 22 października 2026 r.", "SENDER_COMPANY": "Przykładowa Sp. z o.o.",
          "SENDER_ADDRESS": "ul. Zmyślona 7, 50-001 Wrocław", "RECIPIENT_TITLE": "Pan", "RECIPIENT_NAME": "Jan Testowy",
          "RECIPIENT_COMPANY": "Firma Testowa", "RECIPIENT_ADDRESS": "ul. Testowa 1, 00-001 Warszawa",
          "DOCUMENT_TITLE": "Informacja", "GREETING": "Szanowny Panie,", "BODY": "Treść.", "CLOSING": "Z poważaniem,"}


def test_document_uses_the_scan_only_when_chosen_in_the_form(scans, docker):
    import json

    from src import run_context
    from src.agent_tools import _params_path, tool_generate_pdf_from_template

    signatures.save("uzytkownik", tiny_png())
    with run_context.use_context(session_id="sig-1", user="uzytkownik"):
        # a model that slips "_signature" into the parameters changes nothing
        tool_generate_pdf_from_template("letter", {**LETTER, "_signature": signatures.SCAN_LABEL})
        first = sorted(run_context.workspace_dir().glob("letter_*.tex"))[-1]
        assert "includegraphics" not in first.read_text(encoding="utf-8")
        assert json.loads(_params_path(first.name).read_text())["signature"] == "blank"
        tool_generate_pdf_from_template("letter", LETTER, signature="scan")
        second = sorted(run_context.workspace_dir().glob("letter_*.tex"))[-1]
        assert "includegraphics" in second.read_text(encoding="utf-8")
        assert json.loads(_params_path(second.name).read_text())["signature"] == "scan"
        assert list(run_context.workspace_dir().glob("podpis_*.png"))
    with run_context.use_context(session_id="sig-2", user="anna"):  # another user cannot get uzytkownik's scan
        tool_generate_pdf_from_template("letter", LETTER, signature="scan")
        tex = sorted(run_context.workspace_dir().glob("letter_*.tex"))[-1].read_text(encoding="utf-8")
        assert "includegraphics" not in tex and not list(run_context.workspace_dir().glob("podpis_*"))


def test_form_choice_reaches_the_pdf_through_the_document_node(scans, docker, llm):
    from langchain_core.messages import HumanMessage

    import extensions.document_generator.nodes as dg
    from src import run_context
    from src.agent_tools import _load_templates_metadata, build_document_hitl_form

    signatures.save("uzytkownik", tiny_png())
    with run_context.use_context(session_id="sig-3", user="uzytkownik"):
        form = build_document_hitl_form("letter", LETTER, _load_templates_metadata())
        field = next(f for f in form["fields"] if f["id"] == "_signature")
        assert field["value"] == signatures.BLANK_LABEL and signatures.SCAN_LABEL in field["options"]
        state = {"messages": [HumanMessage(content="List do Jana")], "task_plan": {}, "current_step": None,
                 "hitl_response": {"continuation_type": "generate_pdf", "template_id": "letter",
                                   "parameters": {**LETTER, "_signature": signatures.SCAN_LABEL}}}
        out = dg.run_document_generator(state)
        assert out["execution_status"] == "success"
        tex = run_context.workspace_file(out["last_generated_file"]).read_text(encoding="utf-8")
    assert "includegraphics" in tex


def test_side_panel_does_not_list_copied_scans(tmp_path):
    from src import source_view

    (tmp_path / "podpis_0123456789ab.png").write_bytes(tiny_png())
    (tmp_path / "list.pdf").write_bytes(b"%PDF")
    assert set(source_view.snapshot(tmp_path)) == {"list.pdf"}
