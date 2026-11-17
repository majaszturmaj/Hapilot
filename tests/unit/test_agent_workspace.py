"""Per-conversation workspace of the v3 agent (src/agent_workspace.py): folders, read-only mounts, uploads with text
copies, results without links out of the workspace. The container itself is checked on the 4090 (stage 1 report)."""

import os
from pathlib import Path

import pytest

from src import agent_workspace, text_mirror
from src.agent_workspace import Mount, Workspace, WorkspaceError
from src.config import get_settings, load_settings, reset_settings


@pytest.fixture
def env(tmp_path, monkeypatch):
    docs = tmp_path / "dokumenty firmy"
    docs.mkdir()
    monkeypatch.setenv("HAPILOT_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("HAPILOT_DOCS_DIR", str(docs))
    monkeypatch.delenv("HAPILOT_ADDED_DOCS_DIR", raising=False)
    monkeypatch.delenv("HAPILOT_AGENT_WORKSPACE_DIR", raising=False)
    reset_settings()
    yield get_settings()
    reset_settings()


def test_setting_defaults_to_the_data_dir_and_can_be_moved(tmp_path):
    assert load_settings({}).agent_workspace_root == load_settings({}).data_dir / "agent_workspace"
    moved = load_settings({"HAPILOT_AGENT_WORKSPACE_DIR": str(tmp_path / "ws")})
    assert moved.agent_workspace_root == tmp_path / "ws"


def test_folder_name_keeps_plain_ids_and_hashes_others():
    uuid = "3f1c2b7e-9a4d-4c55-8e21-0b6f5d9c7a10"
    assert agent_workspace.folder_name(uuid) == uuid
    odd = agent_workspace.folder_name("../../etc")
    assert odd.startswith("k_") and len(odd) == 34 and odd == agent_workspace.folder_name("../../etc")
    assert agent_workspace.folder_name("a/b") != agent_workspace.folder_name("a_b")
    assert agent_workspace.folder_name("").startswith("k_")


def test_prepare_makes_the_folders_and_mount_points(env):
    ws = Workspace("rozmowa-1").prepare()
    assert ws.dir == env.data_dir / "agent_workspace" / "rozmowa-1"
    for name in ("dokumenty", "dodane", "tekst", "zalaczniki", "wyniki"):
        assert (ws.praca / name).is_dir() and not any((ws.praca / name).iterdir())
    assert ws.uploads.is_dir() and env.added_docs_dir.is_dir()
    assert text_mirror.mirror_dir(env.chroma_path, env.collection).is_dir()


def test_documents_folder_must_exist(env, tmp_path, monkeypatch):
    monkeypatch.setenv("HAPILOT_DOCS_DIR", str(tmp_path / "brak"))
    reset_settings()
    with pytest.raises(WorkspaceError, match="documents folder not found"):
        Workspace("x").prepare()


def test_mounts_are_read_only_except_the_working_directory(env):
    ws = Workspace("rozmowa-1").prepare()
    mounts = {m.target: m for m in ws.mounts()}
    assert list(mounts) == ["/praca", "/praca/dokumenty", "/praca/dodane", "/praca/tekst", "/praca/zalaczniki"]
    assert [m.read_only for m in mounts.values()] == [False, True, True, True, True]
    assert mounts["/praca"].source == ws.praca.resolve()
    assert mounts["/praca/dokumenty"].source == env.docs_dir.resolve()
    assert mounts["/praca/dodane"].source == env.added_docs_dir.resolve()
    assert mounts["/praca/tekst"].source == text_mirror.mirror_dir(env.chroma_path, env.collection).resolve()
    assert mounts["/praca/zalaczniki"].source == ws.uploads.resolve()
    args = ws.docker_args()
    assert args[-2:] == ["--workdir", "/praca"] and args.count("--mount") == 5
    assert f"type=bind,source={env.docs_dir.resolve()},target=/praca/dokumenty,readonly" in args
    assert f"type=bind,source={ws.praca.resolve()},target=/praca" in args  # no "readonly"


def test_mount_argument_quotes_a_path_with_a_comma():
    arg = Mount(Path("/srv/Dokumenty, 2026"), "/praca/dokumenty", True).docker_arg()
    assert arg == 'type=bind,"source=/srv/Dokumenty, 2026",target=/praca/dokumenty,readonly'


def test_index_source_paths_map_to_container_paths():
    assert agent_workspace.document_path("umowy/umowa.pdf") == "/praca/dokumenty/umowy/umowa.pdf"
    assert agent_workspace.document_path("dodane/oferta.docx") == "/praca/dodane/oferta.docx"
    assert agent_workspace.text_path("dodane/oferta.docx") == "/praca/tekst/dodane/oferta.docx.txt"
    assert agent_workspace.text_path("rejestr.xlsx") == "/praca/tekst/rejestr.xlsx.txt"


def test_uploads_get_a_text_copy_and_never_overwrite_each_other(env, tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.title = "Koszty"
    wb.active.append(["Pozycja", "Kwota"])
    wb.active.append(["Papier", 120])
    src = tmp_path / "koszty.xlsx"
    wb.save(src)
    ws = Workspace("rozmowa-2")
    first = ws.add_upload(src)
    assert first["name"] == "koszty.xlsx" and first["path"] == "/praca/zalaczniki/koszty.xlsx" and first["error"] is None
    assert first["text"] == ["/praca/zalaczniki/koszty.xlsx.txt", "/praca/zalaczniki/koszty.xlsx.Koszty.csv"]
    assert (ws.uploads / "koszty.xlsx.Koszty.csv").read_text(encoding="utf-8") == "Pozycja,Kwota\nPapier,120\n"
    assert ws.add_upload(src)["name"] == "koszty_1.xlsx"

    note = tmp_path / "x.txt"
    note.write_text("notatka", encoding="utf-8")
    assert ws.add_upload(note, name="koszty.xlsx.txt")["name"] == "koszty.xlsx_1.txt"  # not the text copy's name
    assert (ws.uploads / "koszty.xlsx.txt").read_text(encoding="utf-8").startswith("[arkusz Koszty]")


def test_upload_names_are_made_safe(env, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("treść", encoding="utf-8")
    info = Workspace("r").add_upload(src, name="../../Umowa najmu (kopia).txt")
    assert info["name"] == "Umowa_najmu_kopia_.txt" and (Workspace("r").uploads / info["name"]).is_file()


def test_unreadable_and_unsupported_uploads_are_said_so(env, tmp_path):
    ws = Workspace("r")
    broken = tmp_path / "skan.pdf"
    broken.write_bytes(b"not a pdf")
    info = ws.add_upload(broken)
    assert info["error"] and info["text"] == ["/praca/zalaczniki/skan.pdf.txt"]
    assert (ws.uploads / "skan.pdf.txt").read_text(encoding="utf-8").startswith("[nie udało się odczytać")
    image = tmp_path / "zdjecie.png"
    image.write_bytes(b"\x89PNG")
    info = ws.add_upload(image)
    assert info["text"] == [] and "no text copy" in info["error"] and (ws.uploads / "zdjecie.png").is_file()


def test_results_list_files_but_no_links_out_of_the_workspace(env, tmp_path):
    ws = Workspace("r").prepare()
    (ws.results_dir / "pismo.pdf").write_bytes(b"%PDF")
    (ws.results_dir / "wykresy").mkdir()
    (ws.results_dir / "wykresy" / "a.png").write_bytes(b"png")
    secret = tmp_path / "tajne.txt"
    secret.write_text("nie dla czatu", encoding="utf-8")
    os.symlink(secret, ws.results_dir / "link.txt")
    os.symlink(tmp_path, ws.results_dir / "folder_link")
    assert [(r["name"], r["bytes"]) for r in ws.results()] == [("pismo.pdf", 4), ("wykresy/a.png", 3)]
    assert Workspace("pusta").results() == []
