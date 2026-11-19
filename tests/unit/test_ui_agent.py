"""What the chat shows for the v3 engine (src/ui_agent.py): tool steps, queue note, approval text, the answer."""

import json
from pathlib import Path

from src.agent_engine import TurnResult
from src.ui_agent import (answer_text, approval_text, describe_input, describe_output, file_list, queue_note,
                          tool_label)


def _result(status="ok", text="Odpowiedź.", error=None, files=()):
    return TurnResult(status=status, text=text, messages=[], files=list(files), error=error, run_dir=Path("."),
                      duration_s=1.0)


def test_tool_steps_read_like_office_work():
    assert tool_label("read_files") == "📖 Czytam pliki" and tool_label("nowe") == "🔧 nowe"
    commands = describe_input("run_commands", json.dumps({"commands": ["ls /praca/tekst", "python3 a.py"]}))
    assert commands.startswith("```bash\nls /praca/tekst\npython3 a.py")
    assert describe_input("semantic_search", json.dumps({"query": "termin reklamacji"})) == "„termin reklamacji”"
    files = describe_input("read_files", json.dumps({"files": [{"path": "/praca/tekst/umowa.pdf.txt"},
                                                               {"path": "/praca/tekst/umowa.pdf.txt"}]}))
    assert files == "- `tekst/umowa.pdf.txt`"  # without /praca, once
    assert "szkic e-maila" in describe_input("email_draft", json.dumps({"to": "a@example.com"}))  # no address here
    assert describe_input("read_files", '{"cut": "…').startswith("```text")  # a cut input stays readable
    assert describe_output("", "brak pliku").startswith("⚠️ brak pliku")
    assert describe_output(json.dumps("linia 1\nlinia 2"), None) == "```text\nlinia 1\nlinia 2\n```"
    assert describe_output("x" * 5000, None).endswith(" …\n```")


def test_queue_note_counts_in_polish():
    assert "**1 zapytanie**" in queue_note(1) and "**3 zapytania**" in queue_note(3)
    assert "**5 zapytań**" in queue_note(5)


def test_approval_shows_the_draft():
    text = approval_text("email_draft", {"to": "anna@example.com", "subject": "Termin", "body": "Dzień dobry,\nok"})
    assert "**Do:** anna@example.com" in text and "> Dzień dobry,\n> ok" in text and "nie wyśle" in text


def test_answer_text_and_files():
    assert answer_text(_result(), "") == "Odpowiedź."
    assert answer_text(_result(text=""), "strumień") == "strumień"
    assert answer_text(_result("error", "", error="model niedostępny"), "").endswith(": model niedostępny")
    stopped = answer_text(_result("aborted", "Liczę."), "")
    assert stopped.endswith("⏹ Przerwano na Twoją prośbę.") and "Liczę." not in stopped  # a working note is no answer
    assert answer_text(_result("aborted", "Liczę."), "Termin to").startswith("Termin to")
    assert "Przekroczony czas" in answer_text(_result("timeout", ""), "")
    assert file_list([]) == "" and "Plik do pobrania" in file_list([{"name": "a"}])
    assert "Pliki do pobrania" in file_list([{"name": "a"}, {"name": "b"}])
