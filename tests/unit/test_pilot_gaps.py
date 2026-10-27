"""Gaps found in the pilot's conversations (2026-10-26): the document list, CSV, the coder's view of data files,
chart follow-ups, letter formulas."""

import json
import re

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from src import data_preview, knowledge_base, run_context
from src.agent_graph import asks_about_chart, asks_for_document_list
from src.config import PROJECT_ROOT
from src.extractors import SUPPORTED, extract_bytes


def plan(*steps):
    return json.dumps({"task_name": "t", "complexity": "low",
                       "steps": [{"id": i + 1, "action": a, "tool": t, "input": inp} for i, (t, a, inp) in enumerate(steps)]})


# --------------------------------------------------------------------------- 4. list of documents

@pytest.mark.parametrize("text,expected", [
    ("wypisz mi listę plików do których masz dostęp", True),          # the pilot's wording
    ("do jakich plików masz dostęp?", True),
    ("dostarcz listę dokumentów w Twojej bazie wiedzy", True),
    ("Do jakich dokumentów i jakiej firmy posiadasz dostęp?", True),
    ("Jakie pliki widzisz?", True),
    ("Potrzebuję wyjść na przepustkę. Jakie dokumenty potrzebuję?", False),  # a question about a procedure
    ("Jakie dokumenty masz o urlopach?", False),                     # a topic: the search answers it
    ("Pokaż listę płac osobowych", False),
    ("Wypisz dokumenty potrzebne do wyjazdu służbowego", False),
])
def test_asks_for_document_list(text, expected):
    assert asks_for_document_list(text) is expected


class Index:
    def __init__(self, chunks):
        self.chunks = chunks

    def all_chunks(self, path, collection):
        return self.chunks

    def dense(self, path, collection, query, k):
        return []


@pytest.fixture
def index():
    chunks = [("a#0", "t", {"source": "regulamin.pdf", "page": 1}), ("a#1", "t", {"source": "regulamin.pdf", "page": 12}),
              ("b#0", "t", {"source": "skan.pdf", "page": 3, "ocr": True}),
              ("c#0", "t", {"source": "rejestr.xlsx", "sheet": "Reklamacje"}),
              ("d#0", "t", {"source": "dodane/notatka.txt"}), ("d#1", "t", {"source": "dodane/notatka.txt"})]
    knowledge_base.set_backend(Index(chunks))
    yield
    knowledge_base.set_backend(None)


def test_documents_are_read_from_the_index(index):
    docs = {d["source"]: d for d in knowledge_base.documents()}
    assert list(docs) == ["dodane/notatka.txt", "regulamin.pdf", "rejestr.xlsx", "skan.pdf"]
    assert docs["regulamin.pdf"]["pages"] == 12 and docs["skan.pdf"]["ocr_pages"] == 1
    assert docs["rejestr.xlsx"]["sheets"] == ["Reklamacje"] and docs["dodane/notatka.txt"]["chunks"] == 2


def test_document_list_question_is_answered_from_the_index(graph, invoke, llm, index):
    """The search only sees the best chunks: the model said "one document" of eleven (pilot)."""
    llm.on("task_planner", plan(("rag_search", "Szukaj listy plików", "lista plików")))
    state = invoke(graph, "wypisz mi listę plików do których masz dostęp", "s-list")
    text = state["messages"][-1].content
    assert text.startswith("📚 **W bazie wiedzy są 4 dokumenty:**")
    assert "**regulamin.pdf** — 12 stron" in text and "**skan.pdf** — 3 strony, w tym 1 odczytana ze skanu" in text
    assert "**notatka.txt** — 2 fragmenty (dodany z czatu)" in text and "arkusz: Reklamacje" in text
    assert not llm.prompts_for("knowledge")  # no search, no model


def test_list_tool_is_routed_but_not_offered_to_the_planner():
    from src.agent_tools import available_planner_tools
    from src.core.extension_manager import get_active

    manager = get_active()
    if not manager.is_loaded():
        manager.discover_and_load()
    assert manager.tool_node_map()["list_documents"] == "document_list"
    assert "list_documents" not in available_planner_tools() and "rag_search" in available_planner_tools()


# --------------------------------------------------------------------------- 5. CSV and accepted formats

def test_csv_is_read_like_a_sheet():
    data = "Data;Stacja;CO\n2026-07-13 12:30;4;0,012\n2026-07-13 12:45;4;0,015\n".encode("cp1250")
    [seg] = extract_bytes("pomiary.csv", data)
    assert seg.text.splitlines()[0] == "Data | Stacja | CO" and seg.meta["header"] == "Data | Stacja | CO"
    assert "2026-07-13 12:45 | 4 | 0,015" in seg.text


def test_long_csv_is_cut_with_a_note(monkeypatch):
    from src import extractors

    monkeypatch.setattr(extractors, "CSV_ROWS", 3)
    data = ("a,b\n" + "".join(f"{i},{i * 2}\n" for i in range(10))).encode()
    [seg] = extract_bytes("duzy.csv", data)
    assert seg.text.splitlines()[:3] == ["a | b", "0 | 0", "1 | 2"]
    assert seg.text.endswith("[… pokazano 3 z 11 wierszy — cały plik może przeanalizować kod Python]")


def test_upload_settings_accept_every_supported_format():
    tomllib = pytest.importorskip("tomllib")
    config = tomllib.loads((PROJECT_ROOT / ".chainlit" / "config.toml").read_text(encoding="utf-8"))
    accept = config["features"]["spontaneous_file_upload"]["accept"]
    endings = {e for exts in accept.values() for e in exts}
    assert endings == set(SUPPORTED)
    assert ".csv" in accept["application/vnd.ms-excel"] and accept["application/vnd.ms-excel"] == [".csv"]  # not .xls


def test_the_model_is_told_which_files_it_accepts():
    from src import llm_config

    llm_config._load_project_context.cache_clear()
    text = llm_config._load_project_context()
    assert "PDF, DOCX, EML, XLSX, CSV, TXT, MD" in text and "Obrazów (PNG, JPG)" in text


# --------------------------------------------------------------------------- 6. the coder sees data files as they are

def _xlsx(rows, path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Dane"
    for row in rows:
        ws.append(row)
    wb.save(path)


def test_csv_lines_inside_excel_cells_are_recognised(tmp_path):
    """The pilot's file: every row one cell with a whole CSV line — five attempts, five empty charts."""
    path = tmp_path / "halda.xlsx"
    _xlsx([[f'4,"Poziom","0.0{i}","m","2026-07-13 12:{i}0:00.000000"'] for i in range(1, 9)], path)
    text = data_preview.preview(path)
    assert "każdy wiersz to JEDNA komórka z całą linią CSV (separator \",\", 5 pól, bez nagłówka" in text
    assert 'pd.read_csv(io.StringIO("\\n".join(raw)), sep=",", header=None)' in text


def test_ordinary_sheet_and_csv_file_show_columns_and_rows(tmp_path):
    sheet = tmp_path / "stacje.xlsx"
    _xlsx([["timestampUtc", "stationId_9_CO", "stationId_10_CO"], ["2026-07-15 00:00", 0.12, 0.2], ["2026-07-15 00:15", 0.13, 0.21]], sheet)
    text = data_preview.preview(sheet)
    assert "kolumny (1. wiersz): timestampUtc | stationId_9_CO | stationId_10_CO" in text and "0.13" in text
    csv_file = tmp_path / "pomiary.csv"
    csv_file.write_text("ID;Pomiar;Wartosc\n1;CO;0,5\n2;Temp;21\n", encoding="utf-8")
    text = data_preview.preview(csv_file)
    assert 'separator ";"' in text and "pierwszy wiersz to nagłówek" in text and "ok. 3 wierszy" in text
    broken = tmp_path / "zepsuty.xlsx"
    broken.write_bytes(b"not a zip")
    assert "nie udało się odczytać struktury" in data_preview.preview(broken)


def test_chart_follow_up_goes_to_code_with_the_conversation(graph, invoke, llm, docker):
    """'no to co z tym wykresem?' went to chat, which promised code and delivered none (pilot)."""
    with run_context.use_context(session_id="s-chart"):
        _xlsx([[f'4,"Poziom","0.0{i}","m","2026-07-13 12:{i}0:00"'] for i in range(1, 9)],
              run_context.workspace_dir() / "halda.xlsx")
    history = [HumanMessage(content="Przygotuj wykres CO w skali logarytmicznej dla każdego ID"),
               AIMessage(content="```python\nimport pandas as pd\ndf = pd.read_excel('halda.xlsx')\n```"),
               HumanMessage(content="WYNIK:\nSUKCES:\nNo numerical data found to plot."),
               AIMessage(content="Nie udało się narysować wykresu."),
               HumanMessage(content="no to co z tym wykresem?")]
    llm.on("task_planner", plan(("chat", "Odpowiedz o wykresie", "no to co z tym wykresem?")))
    llm.on("coder", "```python\nprint('wykres gotowy')\n```")
    state = invoke(graph, history, "s-chart", approval_policy="auto")
    assert state["task_plan"]["steps"][0]["tool"] == "python" and not llm.prompts_for("chat")
    prompt = llm.prompts_for("coder")[0]
    assert "Przygotuj wykres CO w skali logarytmicznej" in prompt               # the earlier request
    assert "pd.read_excel('halda.xlsx')" in prompt                             # the earlier code
    assert "JEDNA komórka z całą linią CSV" in prompt                           # the file as it is


@pytest.mark.parametrize("text,expected", [
    ("no to co z tym wykresem?", True), ("wykres jest pusty", True), ("Nie widzę wykresu", True),
    ("Opisz wyniki bez wykresu", False), ("nie rób wykresu, tylko tabelę", False), ("Napisz e-mail do klienta", False),
])
def test_asks_about_chart(text, expected):
    assert asks_about_chart(text) is expected


def test_chart_mention_without_data_or_code_stays_with_chat(graph, invoke, llm):
    llm.on("task_planner", plan(("chat", "Odpowiedz", "")))
    llm.on("chat", "Wykres kołowy pokazuje udziały.")
    state = invoke(graph, "Co to jest wykres kołowy?", "s-chart-chat")
    assert state["messages"][-1].content == "Wykres kołowy pokazuje udziały."


# --------------------------------------------------------------------------- 7. letter formulas only in letters

def test_writer_uses_letter_formulas_only_in_letters():
    writer = (PROJECT_ROOT / "extensions" / "writer" / "prompts" / "writer.txt").read_text(encoding="utf-8")
    assert "ZWROTY LISTOWE TYLKO W LISTACH" in writer and "Nie dopisuj jednostek" in writer
    rule = re.search(r"ZWROTY LISTOWE TYLKO W LISTACH:(.+)", writer).group(1)
    assert "WYŁĄCZNIE wtedy, gdy użytkownik prosi o list" in rule
