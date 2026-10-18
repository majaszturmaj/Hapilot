"""Regression tests for the planner's keyword guards (word boundaries, negation, existing document)."""

import pytest

from src.agent_graph import is_modification_request, wants_document


@pytest.mark.parametrize("text,has_doc,expected", [
    ("Zmień datę na 1 czerwca 2026 r.", True, True),
    ("Popraw też moje stanowisko na 'Główny Inżynier'.", True, True),
    ("Chcę zmienić adres odbiorcy", True, True),
    ("Zmień datę na 1 czerwca 2026 r.", False, False),          # nothing to modify yet
    ("Ile wynosi zmienna x w tym skrypcie?", True, False),       # "zmienna" is not "zmień"
    ("Czy ten kod jest poprawny?", True, False),                  # "poprawny" is not "popraw"
    ("Napisz to prościej i krócej, po żołniersku.", False, False),
    ("Napisz to prościej i krócej, po żołniersku.", True, False),  # style change of chat text, no document word
    ("Zrób to pismo krócej", True, True),
])
def test_is_modification_request(text, has_doc, expected):
    assert is_modification_request(text, has_doc) is expected


@pytest.mark.parametrize("text,expected", [
    ("Napisz podanie o urlop", True),
    ("Przygotuj podanie w formie PDF-a", True),
    ("Wygeneruj raport z testów", True),
    ("Proszę o protokół z dzisiejszego spotkania", True),
    ("Zrób z tego maila do zespołu. Nie rób z tego PDF, tylko napisz w oknie czatu.", False),
    ("Napisz krótkiego maila. Nie potrzebuję z tego PDF-a, tylko tekst.", False),
    ("Bez PDF, proszę", False),
    ("Proszę przesłać raporty do środy rano do sekretariatu.", False),  # quoted e-mail, plural noun
    ("Jakie są procedury zgłaszania projektu?", False),
    ("Hej! Jak tam ci dzisiaj mija dzień?", False),
])
def test_wants_document(text, expected):
    assert wants_document(text) is expected
