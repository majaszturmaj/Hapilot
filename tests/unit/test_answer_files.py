"""Files created by a request reach the user under the answer, independent of the side panel.

Found on the pilot (2026-10-26): code saved a chart, the side panel of that chat was gone after a crash, nothing
offered the file — and the agent told the user it "cannot send files" and to run the code on their own computer."""

from pathlib import Path

from src import run_context, ui_panel
from src.agent_tools import tool_execute_python
from src.graph_runner import display_text

CHART = ("```python\n"
         "open('wykres_co.png', 'wb').write(b'\\x89PNG\\r\\n\\x1a\\n' + b'0' * 40)\n"
         "open('dane.csv', 'w').write('a;b\\n1;2\\n')\n"
         "print('max 0.51')\n```")


def test_program_result_names_the_files_it_created(docker, session):
    out = tool_execute_python(CHART)
    assert out.startswith("SUKCES") and "max 0.51" in out
    assert "Utworzone pliki: dane.csv, wykres_co.png (system dołącza je pod odpowiedzią" in out
    assert "snippet_" not in out.split("Utworzone pliki")[1]  # the script itself is not a result
    again = tool_execute_python("```python\nprint(open('dane.csv').read())\n```")
    assert "Utworzone pliki" not in again  # reading files changes nothing


def test_display_shows_the_files_outside_the_program_output():
    shown = display_text("WYNIK:\nSUKCES:\nmax 0.51\n\nUtworzone pliki: wykres_co.png (system dołącza je pod odpowiedzią"
                         " — użytkownik może je obejrzeć i pobrać)")
    assert shown.startswith("🐍 **Wynik programu:**\n\n```text\nmax 0.51\n```")
    assert shown.endswith("📎 Pliki utworzone przez program: **wykres_co.png** — poniżej.")
    assert display_text("WYNIK:\nSUKCES:\n42") == "🐍 **Wynik programu:**\n\n```text\n42\n```"


def _workspace(session) -> Path:
    with run_context.use_context(session_id=session):
        return run_context.workspace_dir()


def test_created_files_are_attached_but_not_helpers_or_uploads(session):
    ws = _workspace(session)
    (ws / "pomiary.xlsx").write_bytes(b"upload")  # the user's own file, in the workspace before the request
    pending = ui_panel.start_turn(session, "zrób wykres")
    (ws / "wykres_co.png").write_bytes(b"\x89PNG")
    (ws / "snippet_20261026_085708_1.py").write_text("print(1)")
    (ws / "list.pdf").write_bytes(b"%PDF")
    (ws / "list.tex").write_text("\\documentclass{article}")
    files = ui_panel.answer_files(session, pending, "Wykres zapisałem jako wykres_co.png. Dane z pomiary.xlsx.",
                                  uploads=["pomiary.xlsx"])
    assert [p.name for p in files] == ["list.pdf", "wykres_co.png"]


def test_an_earlier_file_named_in_the_answer_is_attached_again(session):
    """'Can you send me the chart?' — the answer names the file, so it comes with the answer."""
    ws = _workspace(session)
    (ws / "co_moving_averages.png").write_bytes(b"\x89PNG")
    (ws / "dane.xlsx").write_bytes(b"upload")
    pending = ui_panel.start_turn(session, "możesz go przesłać?")
    files = ui_panel.answer_files(session, pending, "Oto wykres `co_moving_averages.png` (z pliku dane.xlsx).",
                                  uploads=["dane.xlsx"])
    assert [p.name for p in files] == ["co_moving_averages.png"]
    assert ui_panel.answer_files(session, None, "Bez plików.", uploads=[]) == []


def test_at_most_ten_files(session):
    ws = _workspace(session)
    pending = ui_panel.start_turn(session, "dużo wykresów")
    for i in range(14):
        (ws / f"wykres_{i:02d}.png").write_bytes(b"\x89PNG")
    assert len(ui_panel.answer_files(session, pending, "", uploads=[])) == ui_panel.ATTACH_LIMIT


def test_prompts_tell_the_model_about_files_and_unseen_charts():
    from src.config import PROJECT_ROOT

    ext = PROJECT_ROOT / "extensions"
    writer = (ext / "writer" / "prompts" / "writer.txt").read_text(encoding="utf-8")
    chat = (ext / "chat" / "prompts" / "chat.txt").read_text(encoding="utf-8")
    assert "Wykresów ani obrazów NIE widzisz" in writer and "Nigdy nie pisz, że nie możesz przesłać pliku" in writer
    assert "Nigdy nie pisz, że nie możesz przesłać pliku" in chat and "podaj jego dokładną nazwę" in chat
    for name in ("coder_qwen.txt", "coder_bielik.txt"):
        coder = (ext / "python_coder" / "prompts" / name).read_text(encoding="utf-8")
        assert "plt.savefig" in coder and "plt.show()" in coder
