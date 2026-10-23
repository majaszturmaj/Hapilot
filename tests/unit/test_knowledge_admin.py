"""Knowledge base in the agent panel (src/knowledge_admin.py, src/ingest_data.py): administrators only, uploads into
the company folder, replace / delete through a trash, restore, the folder re-check, the per-document list, and the
next ``ingest`` of an unchanged folder staying a no-op. In-memory stand-in for Chroma."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import admin, ingest_data, knowledge_admin, knowledge_base, ui_activity
from src.config import get_settings, reset_settings


class FakeCollection:
    """What the code uses of a Chroma collection (and of LangChain's store on top of it)."""

    def __init__(self):
        self.rows: dict[str, tuple[str, dict]] = {}
        self.adds = 0

    def add_documents(self, docs, ids):
        self.adds += 1
        for doc, cid in zip(docs, ids):
            self.rows[cid] = (doc.page_content, dict(doc.metadata))

    def get(self, include=None, where=None):
        rows = [(cid, text, meta) for cid, (text, meta) in self.rows.items()
                if not where or all(meta.get(k) == v for k, v in where.items())]
        return {"ids": [r[0] for r in rows], "documents": [r[1] for r in rows], "metadatas": [r[2] for r in rows]}

    def delete(self, where):
        for cid in self.get(where=where)["ids"]:
            del self.rows[cid]

    def sources(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for text, meta in self.rows.values():
            out.setdefault(meta["source"], []).append(text)
        return out


class FakeClient:
    def __init__(self, collection):
        self.collection = collection
        self.rebuilt = 0

    def get_collection(self, name):
        return self.collection

    def delete_collection(self, name):
        self.rebuilt += 1
        self.collection.rows.clear()


@pytest.fixture
def kb(tmp_path, monkeypatch):
    data, docs = tmp_path / "var", tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setenv("HAPILOT_DATA_DIR", str(data))
    monkeypatch.setenv("HAPILOT_DOCS_DIR", str(docs))
    monkeypatch.delenv("HAPILOT_ADDED_DOCS_DIR", raising=False)
    monkeypatch.delenv("HAPILOT_UI_ADMINS", raising=False)  # default: "admin"
    reset_settings()
    collection = FakeCollection()
    client = FakeClient(collection)
    monkeypatch.setattr(knowledge_base, "chroma_client", lambda path=None: client)
    monkeypatch.setattr(knowledge_base, "vector_store", lambda collection=None, path=None: client.collection)
    monkeypatch.setattr(knowledge_admin, "_queue", type(knowledge_admin._queue)())
    monkeypatch.setattr(knowledge_admin, "_errors", {})
    (docs / "regulamin.txt").write_text("Urlop zgłaszamy 14 dni wcześniej w systemie kadrowym.", encoding="utf-8")
    s = get_settings()
    ingest_data.ingest(docs, s.collection, path=s.chroma_path, added_dir=s.added_docs_dir)
    client.rebuilt = 0
    yield {"docs": docs, "data": data, "index": collection, "client": client}
    reset_settings()


def _file(tmp_path, name, text):
    path = tmp_path / ("upload-" + name)
    path.write_text(text, encoding="utf-8")
    return path


def _by_source(overview):
    return {f["source"]: f for f in overview["files"]}


def _ingest_again():
    s = get_settings()
    return ingest_data.ingest(s.docs_dir, s.collection, path=s.chroma_path, added_dir=s.added_docs_dir)


def test_upload_goes_into_the_company_folder_and_is_indexed_in_the_background(kb, tmp_path):
    upload = _file(tmp_path, "parking.txt", "Goście parkują w strefie B przy portierni.")
    assert knowledge_admin.upload("parking.txt", upload, "admin") == {"source": "parking.txt", "replaced": False}
    assert (kb["docs"] / "parking.txt").read_text(encoding="utf-8").startswith("Goście") and not upload.exists()
    assert _by_source(knowledge_admin.overview("admin"))["parking.txt"]["status"] == "queued"
    assert "parking.txt" not in kb["index"].sources()

    assert knowledge_admin.run_pending() == 1
    assert kb["index"].sources()["parking.txt"] == ["Goście parkują w strefie B przy portierni."]
    entry = _by_source(knowledge_admin.overview("admin"))["parking.txt"]
    assert entry["status"] == "indexed" and entry["chunks"] == 1 and entry["origin"] == "firma"
    assert (entry["added_by"], entry["via"], entry["type"]) == ("admin", "panel", "Tekst")
    assert admin.audit_log("admin")[0]["action"] == "kb_uploaded"

    assert _ingest_again()["chunks"] == 2 and kb["client"].rebuilt == 0  # the folder matches the index: no rebuild


def test_only_administrators(kb, tmp_path):
    upload = _file(tmp_path, "x.txt", "Treść.")
    for call in (lambda: knowledge_admin.overview("uzytkownik"), lambda: knowledge_admin.upload("x.txt", upload, "uzytkownik"),
                 lambda: knowledge_admin.delete("regulamin.txt", "uzytkownik"),
                 lambda: knowledge_admin.text("regulamin.txt", "uzytkownik"),
                 lambda: knowledge_admin.file_for("regulamin.txt", "uzytkownik")):
        with pytest.raises(knowledge_admin.KnowledgeError, match="administrator"):
            call()
    assert not upload.exists()  # a refused upload is not kept
    assert (kb["docs"] / "regulamin.txt").is_file()


def test_same_name_asks_to_replace_and_the_old_version_goes_to_the_trash(kb, tmp_path):
    with pytest.raises(knowledge_admin.KnowledgeConflict, match="Zastąpić"):
        knowledge_admin.upload("regulamin.txt", _file(tmp_path, "a", "Urlop zgłaszamy 7 dni wcześniej."), "admin")
    result = knowledge_admin.upload("regulamin.txt", _file(tmp_path, "b", "Urlop zgłaszamy 7 dni wcześniej."), "admin",
                                    replace=True)
    assert result["replaced"] is True
    knowledge_admin.run_pending()
    assert kb["index"].sources()["regulamin.txt"] == ["Urlop zgłaszamy 7 dni wcześniej."]  # old chunks are gone
    (old,) = knowledge_admin.overview("admin")["trash"]
    assert (old["source"], old["reason"], old["by"], old["days_left"]) == ("regulamin.txt", "replaced", "admin", 30)
    entry = _by_source(knowledge_admin.overview("admin"))["regulamin.txt"]
    assert (entry["via"], entry["replaced_by"]) == ("folder", "admin")  # came with the folder, replaced in the panel

    with pytest.raises(knowledge_admin.KnowledgeError, match="taka sama"):
        knowledge_admin.replace_document("regulamin.txt", _file(tmp_path, "c", "Urlop zgłaszamy 7 dni wcześniej."), "admin")


def test_the_same_content_under_another_name_is_refused(kb, tmp_path):
    copy = _file(tmp_path, "kopia", (kb["docs"] / "regulamin.txt").read_text(encoding="utf-8"))
    with pytest.raises(knowledge_admin.KnowledgeError, match="Ten sam dokument jest już w bazie wiedzy: regulamin.txt"):
        knowledge_admin.upload("kopia.txt", copy, "admin")
    assert not (kb["docs"] / "kopia.txt").exists()


def test_delete_moves_to_the_trash_and_restore_brings_it_back(kb, tmp_path):
    knowledge_admin.upload("parking.txt", _file(tmp_path, "p", "Goście parkują w strefie B."), "admin")
    knowledge_admin.run_pending()
    knowledge_admin.delete("parking.txt", "admin")
    assert not (kb["docs"] / "parking.txt").exists()
    knowledge_admin.run_pending()
    assert "parking.txt" not in kb["index"].sources() and "parking.txt" not in _by_source(knowledge_admin.overview("admin"))
    (entry,) = knowledge_admin.overview("admin")["trash"]
    assert entry["reason"] == "deleted" and oct(os.stat(knowledge_admin.trash_dir()).st_mode & 0o777) == "0o700"
    assert _ingest_again()["files"] == 1 and kb["client"].rebuilt == 0

    knowledge_admin.restore(entry["id"], "admin")
    knowledge_admin.run_pending()
    restored = _by_source(knowledge_admin.overview("admin"))["parking.txt"]
    assert restored["status"] == "indexed" and restored["added_by"] == "admin"  # who added it is kept
    assert kb["index"].sources()["parking.txt"] and knowledge_admin.overview("admin")["trash"] == []
    with pytest.raises(knowledge_admin.KnowledgeError, match="koszu"):
        knowledge_admin.restore(entry["id"], "admin")


def test_restoring_an_older_version_puts_the_newer_one_into_the_trash(kb, tmp_path):
    knowledge_admin.replace_document("regulamin.txt", _file(tmp_path, "v2", "Wersja druga regulaminu."), "admin")
    (older,) = knowledge_admin.overview("admin")["trash"]
    result = knowledge_admin.restore(older["id"], "admin")
    knowledge_admin.run_pending()
    assert result == {"restored": "regulamin.txt", "replaced": True}
    assert "14 dni" in (kb["docs"] / "regulamin.txt").read_text(encoding="utf-8")
    assert "14 dni" in kb["index"].sources()["regulamin.txt"][0]
    (newer,) = knowledge_admin.overview("admin")["trash"]
    assert newer["reason"] == "replaced" and newer["id"] != older["id"]


def test_old_trash_entries_are_deleted_for_good(kb, tmp_path):
    knowledge_admin.delete("regulamin.txt", "admin")
    (entry,) = knowledge_admin.trash()
    assert knowledge_admin.purge_trash(datetime.now(timezone.utc) + timedelta(days=29)) == 0
    assert knowledge_admin.purge_trash(datetime.now(timezone.utc) + timedelta(days=31)) == 1
    assert knowledge_admin.trash() == [] and not (knowledge_admin.trash_dir() / entry["id"]).exists()


def test_folder_recheck_picks_up_files_changed_on_the_server(kb, tmp_path):
    (kb["docs"] / "kadry").mkdir()
    (kb["docs"] / "kadry" / "bhp.txt").write_text("Kask nosimy na hali produkcyjnej.", encoding="utf-8")
    (kb["docs"] / "regulamin.txt").write_text("Urlop zgłaszamy 21 dni wcześniej.", encoding="utf-8")
    assert _by_source(knowledge_admin.overview("admin"))["kadry/bhp.txt"]["status"] == "pending"
    scan = knowledge_admin.rescan()
    assert (scan["added"], scan["changed"], scan["removed"]) == (1, 1, 0)
    assert kb["index"].sources()["kadry/bhp.txt"] == ["Kask nosimy na hali produkcyjnej."]
    assert kb["index"].sources()["regulamin.txt"] == ["Urlop zgłaszamy 21 dni wcześniej."]
    entry = _by_source(knowledge_admin.overview("admin"))["kadry/bhp.txt"]
    assert (entry["status"], entry["folder"], entry["via"], entry["added_by"]) == ("indexed", "kadry", "folder", "system")
    assert {e["action"] for e in admin.audit_log("admin")} == {"kb_folder_added", "kb_folder_changed"}

    (kb["docs"] / "kadry" / "bhp.txt").unlink()
    assert knowledge_admin.rescan()["removed"] == 1 and "kadry/bhp.txt" not in kb["index"].sources()
    assert _ingest_again()["files"] == 1 and kb["client"].rebuilt == 0


def test_a_recheck_without_changes_does_nothing_and_does_not_keep_the_ui_busy(kb):
    """The updater restarts the UI only after 10 minutes without activity: a re-check every 5 minutes that counted
    as activity would postpone every update for ever."""
    status = ui_activity.status_file()
    status.unlink(missing_ok=True)
    adds = kb["index"].adds
    scan = knowledge_admin.rescan()
    assert (scan["added"], scan["changed"], scan["removed"]) == (0, 0, 0)
    assert kb["index"].adds == adds and not status.exists()


def test_an_index_from_an_older_hapilot_is_listed_without_reindexing(kb):
    s = get_settings()
    ingest_data.files_path(s.collection, s.chroma_path).unlink()  # built before the per-document list existed
    adds = kb["index"].adds
    assert knowledge_admin.rescan()["added"] == 0 and kb["index"].adds == adds
    assert _by_source(knowledge_admin.overview("admin"))["regulamin.txt"]["status"] == "indexed"


def test_a_list_out_of_step_with_the_index_is_rebuilt_and_documents_indexed_again(kb):
    s = get_settings()
    (kb["docs"] / "regulamin.txt").write_text("Zmieniony w czasie, gdy działała starsza wersja.", encoding="utf-8")
    marker = s.chroma_path / f"{s.collection}.fingerprint.json"
    data = json.loads(marker.read_text())
    data["fingerprint"] = "rebuilt-by-an-older-version"
    marker.write_text(json.dumps(data))
    scan = knowledge_admin.rescan()
    assert scan["changed"] + scan["added"] == 1
    assert kb["index"].sources()["regulamin.txt"] == ["Zmieniony w czasie, gdy działała starsza wersja."]


def test_chat_additions_are_listed_with_their_author(kb, tmp_path):
    s = get_settings()
    upload = _file(tmp_path, "notatka.txt", "Spotkanie zespołu we wtorek o 9:00.")
    result = ingest_data.add_documents([upload], docs_dir=s.docs_dir, added_dir=s.added_docs_dir,
                                       collection=s.collection, path=s.chroma_path)
    knowledge_admin.record_added(result["added"], "uzytkownik")
    entry = _by_source(knowledge_admin.overview("admin"))[result["added"][0]]
    assert (entry["origin"], entry["via"], entry["added_by"], entry["status"]) == ("dodane", "czat", "uzytkownik", "indexed")
    assert knowledge_admin.rescan()["added"] == 0  # already in the per-document list
    knowledge_admin.delete(result["added"][0], "admin")  # administrators may delete any document
    knowledge_admin.run_pending()
    assert result["added"][0] not in kb["index"].sources()


@pytest.mark.parametrize("name, message", [
    ("../../etc/passwd.txt", None),  # only the file name counts
    (".ukryty.txt", "Niepoprawna nazwa"),
    ("program.exe", "Nieobsługiwany typ"),
    ("a<b>.txt", "Niepoprawna nazwa"),
    ("x" * 130 + ".txt", "Niepoprawna nazwa"),
])
def test_file_names(name, message):
    if message is None:
        assert knowledge_admin.check_name(name) == "passwd.txt"
    else:
        with pytest.raises(knowledge_admin.KnowledgeError, match=message):
            knowledge_admin.check_name(name)


def test_content_must_match_the_type_and_sources_stay_inside_the_folders(kb, tmp_path):
    with pytest.raises(knowledge_admin.KnowledgeError, match="nie pasuje"):
        knowledge_admin.upload("skan.pdf", _file(tmp_path, "s", "to nie jest PDF"), "admin")
    for source in ("../var/knowledge_base/registry.json", "/etc/passwd", "dodane/../../docs/regulamin.txt", "brak.txt"):
        with pytest.raises(knowledge_admin.KnowledgeError, match="Nie ma takiego dokumentu"):
            knowledge_admin.file_for(source, "admin")


def test_demo_documents_in_the_checkout_are_shown_but_never_changed(kb, tmp_path, monkeypatch):
    monkeypatch.delenv("HAPILOT_DOCS_DIR")  # default: the demo documents of the repository
    reset_settings()
    overview = knowledge_admin.overview("admin")
    assert overview["writable"] is False and all(not f["writable"] for f in overview["files"] if f["origin"] == "firma")
    with pytest.raises(knowledge_admin.KnowledgeError, match="przykładowe dokumenty"):
        knowledge_admin.upload("nowy.txt", _file(tmp_path, "n", "Nowy dokument."), "admin")
    demo = next(f["source"] for f in overview["files"] if f["origin"] == "firma")
    with pytest.raises(knowledge_admin.KnowledgeError, match="przykładowe dokumenty"):
        knowledge_admin.delete(demo, "admin")


def test_text_preview_per_page_and_the_original_file(kb):
    from src.config import PROJECT_ROOT

    pdf = next((PROJECT_ROOT / "examples" / "demo_company" / "docs").rglob("*.pdf"))
    (kb["docs"] / "raport.pdf").write_bytes(pdf.read_bytes())
    preview = knowledge_admin.text("raport.pdf", "admin")
    assert preview["error"] is None and preview["segments"][0]["label"] == "Strona 1" and preview["segments"][0]["text"]
    assert knowledge_admin.file_for("raport.pdf", "admin") == (kb["docs"] / "raport.pdf").resolve()
    knowledge_admin.rescan()
    entry = _by_source(knowledge_admin.overview("admin"))["raport.pdf"]
    assert entry["pages"] >= 1 and entry["chunks"] >= 1 and entry["type"] == "PDF"


def test_an_unreadable_file_stays_listed_with_the_reason(kb):
    (kb["docs"] / "pusty.pdf").write_bytes(b"%PDF-1.4 but nothing more")
    knowledge_admin.rescan()
    entry = _by_source(knowledge_admin.overview("admin"))["pusty.pdf"]
    assert entry["status"] == "error" and entry["error"] and entry["chunks"] == 0
    assert knowledge_admin.rescan()["changed"] == 0  # not read again until the file changes


def test_indexing_counts_as_ui_activity_and_a_failure_is_shown(kb, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ingest_data, "index_entries", lambda *a, **k: seen.append(json.loads(
        ui_activity.status_file().read_text())["active"]) or (_ for _ in ()).throw(RuntimeError("dysk pełny")))
    knowledge_admin.upload("parking.txt", _file(tmp_path, "p", "Goście parkują w strefie B."), "admin")
    knowledge_admin.run_pending()
    assert seen == [1]
    entry = _by_source(knowledge_admin.overview("admin"))["parking.txt"]
    assert entry["status"] == "error" and "dysk pełny" in entry["error"]


def test_upload_settings(monkeypatch):
    from src.config import ConfigError, load_settings

    base = {"HAPILOT_PROFILES_FILE": os.environ["HAPILOT_PROFILES_FILE"]}
    s = load_settings(base)
    assert (s.kb_max_upload_mb, s.kb_rescan_minutes, s.kb_trash_days) == (50, 5.0, 30)
    s = load_settings({**base, "HAPILOT_KB_MAX_UPLOAD_MB": "20", "HAPILOT_KB_RESCAN_MINUTES": "0", "HAPILOT_KB_TRASH_DAYS": "7"})
    assert (s.kb_max_upload_mb, s.kb_rescan_minutes, s.kb_trash_days) == (20, 0.0, 7)
    for name, value in (("HAPILOT_KB_MAX_UPLOAD_MB", "0"), ("HAPILOT_KB_RESCAN_MINUTES", "pięć"), ("HAPILOT_KB_TRASH_DAYS", "-1")):
        with pytest.raises(ConfigError, match=name):
            load_settings({**base, name: value})


def test_ingest_from_the_launcher_refuses_while_the_ui_runs(monkeypatch):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import argparse

    import hapilotctl

    ran = []
    monkeypatch.setattr(hapilotctl.subprocess, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(hapilotctl, "owned", lambda name: {"pid": 4242} if name == "ui" else None)
    with pytest.raises(SystemExit, match="keeps the knowledge base up to date itself"):
        hapilotctl.cmd_ingest(argparse.Namespace(docs=None, force=True))
    assert ran == []
    monkeypatch.setattr(hapilotctl, "models_up", lambda profile: None)
    monkeypatch.setattr(hapilotctl, "ui_up", lambda profile: None)
    hapilotctl.cmd_up(argparse.Namespace(profile=None))  # `up` with the UI running: no indexing, no error
    assert ran == []
    monkeypatch.setattr(hapilotctl, "owned", lambda name: None)
    hapilotctl.cmd_ingest(argparse.Namespace(docs=None, force=False))
    assert len(ran) == 1


# --------------------------------------------------------------------------- API routes (need starlette: the GPU host)

@pytest.fixture
def api(kb, monkeypatch):
    pytest.importorskip("starlette")
    pytest.importorskip("httpx")
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from src import ui_admin

    monkeypatch.setattr(ui_admin, "_identifier", lambda request: request.headers.get("x-test-user"))
    app = Starlette(routes=[Route(ui_admin.PREFIX + p, e, methods=m) for p, e, m in ui_admin.ROUTES])
    client = TestClient(app, base_url="https://hapilot.example.test")
    client.headers.update({"x-test-user": "admin", "x-hapilot-admin": "1"})
    return client


KB = "/hapilot/admin/api/kb"


def test_api_is_for_administrators_and_writes_need_the_panel_header(api):
    assert api.get(KB, headers={"x-test-user": ""}).status_code == 401
    assert api.get(KB, headers={"x-test-user": "uzytkownik"}).status_code == 403
    assert api.get(KB + "/file?source=regulamin.txt", headers={"x-test-user": "uzytkownik"}).status_code == 403
    assert api.post(KB + "/upload?name=a.txt", content=b"Tekst.", headers={"x-hapilot-admin": ""}).status_code == 403
    assert api.post(KB + "/upload?name=a.txt", content=b"Tekst.", headers={"origin": "https://evil.example.com"}).status_code == 403
    assert api.get(KB).json()["totals"]["files"] == 1


def test_api_upload_conflict_download_delete_restore(api, kb):
    res = api.post(KB + "/upload?name=parking.txt", content="Goście parkują w strefie B.".encode())
    assert res.status_code == 200 and res.json()["source"] == "parking.txt"
    conflict = api.post(KB + "/upload?name=parking.txt", content="Goście parkują w strefie C.".encode())
    assert conflict.status_code == 409 and "Zastąpić" in conflict.json()["error"]
    assert api.post(KB + "/upload?name=parking.txt&replace=1", content="Goście parkują w strefie C.".encode()).status_code == 200
    knowledge_admin.run_pending()

    down = api.get(KB + "/file?source=parking.txt")
    assert down.text == "Goście parkują w strefie C." and down.headers["content-disposition"].startswith("attachment")
    shown = api.get(KB + "/file?source=parking.txt&inline=1")
    assert shown.headers["content-disposition"].startswith("inline") and shown.headers["content-type"].startswith("text/plain")
    assert shown.headers["x-content-type-options"] == "nosniff"
    assert api.get(KB + "/file?source=../var/knowledge_base/trash.json").status_code == 404
    assert api.get(KB + "/text?source=parking.txt").json()["segments"][0]["text"] == "Goście parkują w strefie C."

    assert api.put(KB + "/file?source=parking.txt", content="Goście parkują w strefie D.".encode()).status_code == 200
    assert api.delete(KB + "/file?source=parking.txt").status_code == 200
    trash = api.get(KB).json()["trash"]
    assert [t["reason"] for t in trash] == ["deleted", "replaced", "replaced"]
    assert api.post(KB + f"/trash/restore?id={trash[0]['id']}").status_code == 200
    assert (kb["docs"] / "parking.txt").read_text(encoding="utf-8") == "Goście parkują w strefie D."
    assert api.post(KB + "/rescan").json() == {"queued": True}


def test_api_refuses_a_file_over_the_limit_and_keeps_nothing(api, kb, monkeypatch):
    monkeypatch.setenv("HAPILOT_KB_MAX_UPLOAD_MB", "1")
    reset_settings()
    big = b"a" * (1024 * 1024 + 1)
    res = api.post(KB + "/upload?name=duzy.txt", content=big)
    assert res.status_code == 413 and "najwyżej 1 MB" in res.json()["error"]

    def stream():  # no Content-Length: counted while it arrives
        yield big[:600_000]
        yield big[600_000:]

    assert api.post(KB + "/upload?name=duzy.txt", content=stream()).status_code == 413
    assert not (kb["docs"] / "duzy.txt").exists() and list((knowledge_admin.folder() / "incoming").iterdir()) == []
