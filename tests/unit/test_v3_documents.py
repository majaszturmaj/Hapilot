"""Engine v3: a PDF is a draft the user checks in the v2 form before it is made (src/agent_documents.py), and a PDF
under an answer opens in the right-hand panel.

Asked on the pilot (2026-11-23): under v2 the fields were filled in first and could be edited before "Generuj PDF";
under v3 the agent made the PDF at once, and clicking it under the answer opened a save dialog."""

import json

import pytest

from src import agent_documents
from src.agent_bridge import Bridge
from src.agent_workspace import Workspace
from src.ui_agent import file_element, file_list

LETTER = {"SENDER_NAME": "Jan Kowalski", "CITY_DATE": "Wrocław, 23 listopada 2026 r.", "SENDER_COMPANY": "Demo Sp. z o.o.",
          "SENDER_ADDRESS": "ul. Przykładowa 1, 50-001 Wrocław", "RECIPIENT_NAME": "Anna Nowak",
          "RECIPIENT_COMPANY": "Przykładowa Sp. z o.o.", "RECIPIENT_ADDRESS": "ul. Zmyślona 7, 50-001 Wrocław",
          "DOCUMENT_TITLE": "Wniosek o urlop", "GREETING": "Szanowna Pani,",
          "BODY": "Proszę o urlop od 1 do 5 grudnia 2026 r.", "CLOSING": "Z poważaniem"}


@pytest.fixture
def ws(tmp_path):
    return Workspace("rozmowa", root=tmp_path / "ws")


# --------------------------------------------------------------------------- the bridge in the chat

def test_chat_create_saves_a_draft_and_makes_no_pdf(ws, docker):
    bridge = Bridge(ws, pdf_drafts=True)
    note = bridge.handle("pdf_document", {"action": "create", "template_id": "letter", "file_name": "wniosek urlop",
                                          "parameters": {**LETTER, "_signature": "Mój skan"}})
    assert note.startswith("SZKIC:") and "Generuj PDF" in note and "Nie wywołuj pdf_document ponownie" in note
    assert ws.results() == []  # nothing is compiled before the user's "Generuj PDF"
    assert bridge.drafts == [{"id": "szkic-1", "template_id": "letter"}]
    draft = agent_documents.load(ws, "szkic-1")
    assert draft["template_id"] == "letter" and draft["parameters"]["BODY"] == LETTER["BODY"]
    assert "_signature" not in draft["parameters"]  # the signature is chosen by the user in the form only
    assert draft["file_name"] == "wniosek urlop"
    second = Bridge(ws, pdf_drafts=True)
    second.handle("pdf_document", {"action": "create", "template_id": "letter", "parameters": LETTER})
    assert second.drafts[0]["id"] == "szkic-2"


def test_chat_create_with_an_unknown_template_says_so(ws, docker):
    bridge = Bridge(ws, pdf_drafts=True)
    assert "Nieznany szablon" in bridge.handle("pdf_document", {"action": "create", "template_id": "brak",
                                                                "parameters": {}})
    assert bridge.drafts == []


def test_without_drafts_the_pdf_is_made_at_once_as_before(ws, docker):
    result = Bridge(ws).handle("pdf_document", {"action": "create", "template_id": "letter", "parameters": LETTER})
    assert result.startswith("SUKCES: zapisano /praca/wyniki/letter.pdf"), result
    assert [r["name"] for r in ws.results()] == ["letter.pdf"]
    assert agent_documents.load(ws, "letter.pdf") is None  # evaluation runs keep nothing for "edit again"


# --------------------------------------------------------------------------- the form and "Generuj PDF"

def test_form_has_the_draft_values_and_the_template(ws, docker):
    Bridge(ws, pdf_drafts=True).handle("pdf_document", {"action": "create", "template_id": "letter",
                                                        "parameters": LETTER})
    fields = {f["id"]: f for f in agent_documents.form(None, agent_documents.load(ws, "szkic-1"))["fields"]}
    assert fields["_template_id"]["value"] == "letter"
    assert fields["BODY"]["value"] == LETTER["BODY"] and fields["RECIPIENT_NAME"]["value"] == "Anna Nowak"


def test_generate_makes_the_pdf_and_keeps_its_values_for_a_new_version(ws, docker):
    edited = {**LETTER, "BODY": "Proszę o urlop od 2 do 6 grudnia 2026 r.", "_signature": "x"}
    path, result = agent_documents.make_pdf(ws, None, "letter", edited, "blank", "wniosek urlop")
    assert path is not None and path.name == "wniosek_urlop.pdf" and result.startswith("SUKCES"), result
    assert [r["name"] for r in ws.results()] == ["wniosek_urlop.pdf"]  # only the PDF: values live outside /praca
    kept = agent_documents.load(ws, "wniosek_urlop.pdf")
    assert kept["parameters"]["BODY"].startswith("Proszę o urlop od 2") and "_signature" not in kept["parameters"]
    assert kept["signature"] == "blank" and kept["file_name"] == "wniosek_urlop.pdf"
    again, _ = agent_documents.make_pdf(ws, None, "letter", kept["parameters"], kept["signature"], kept["file_name"])
    assert again.name == "wniosek_urlop_1.pdf" and path.is_file()  # a new version; the old one stays


def test_generate_errors_are_the_v2_messages(ws, docker):
    path, result = agent_documents.make_pdf(ws, None, "letter", {"SENDER_NAME": "Jan"}, "blank")
    assert path is None and result.startswith("BŁĄD: Brakujące wymagane parametry") and ws.results() == []


def test_ids_cannot_leave_the_folder(ws):
    for bad in ("../x", "a/b", "", ".ukryty", None, 7):
        assert agent_documents.load(ws, bad) is None
    folder = ws.dir / agent_documents.DATA_DIR
    folder.mkdir(parents=True)
    (folder / "zly.json").write_text("{nie json")
    (folder / "pusty.json").write_text(json.dumps({"parameters": {}}))
    assert agent_documents.load(ws, "zly") is None and agent_documents.load(ws, "pusty") is None


# --------------------------------------------------------------------------- a PDF under the answer

class FakeCl:
    class _Element:
        def __init__(self, name, path, display):
            self.name, self.path, self.display = name, path, display

    class Pdf(_Element):
        pass

    class Image(_Element):
        pass

    class File(_Element):
        pass


def test_pdf_opens_on_the_right_other_files_download():
    pdf = file_element(FakeCl, "wniosek.pdf", "/x/wniosek.pdf")
    assert isinstance(pdf, FakeCl.Pdf) and pdf.display == "side"
    assert isinstance(file_element(FakeCl, "wykres.png", "/x/w.png"), FakeCl.Image)
    assert isinstance(file_element(FakeCl, "dane.csv", "/x/d.csv"), FakeCl.File)


def test_the_answer_names_the_pdf_so_its_name_is_the_link():
    note = file_list([{"name": "wyniki/wniosek.PDF"}, {"name": "dane.csv"}])
    assert "Kliknij nazwę, aby otworzyć dokument po prawej: wniosek.PDF" in note and "Plik do pobrania" in note
    assert "Pliki do pobrania" not in file_list([{"name": "a.pdf"}, {"name": "b.pdf"}])
