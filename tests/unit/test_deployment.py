"""Long-running installs: UI on the local network (address, HTTPS), the activity file, and the updater that follows
a git branch (back up, test, switch, restart, roll back)."""

import asyncio
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import hapilotctl  # noqa: E402
from src import ui_activity  # noqa: E402
from src.config import ConfigError, load_settings  # noqa: E402


# --------------------------------------------------------------------------- settings

def test_ui_binds_localhost_by_default_and_accepts_an_address():
    defaults = load_settings({})
    assert (defaults.ui_host, defaults.ui_ssl_cert, defaults.ui_ssl_key, defaults.update_branch) == \
        ("127.0.0.1", None, None, "main")
    lan = load_settings({"HAPILOT_UI_HOST": "0.0.0.0", "HAPILOT_UI_SSL_CERT": "/etc/x.crt", "HAPILOT_UI_SSL_KEY": "/etc/x.key",
                         "HAPILOT_UPDATE_BRANCH": "pilot"})
    assert (lan.ui_host, lan.ui_ssl_cert, lan.ui_ssl_key, lan.update_branch) == \
        ("0.0.0.0", Path("/etc/x.crt"), Path("/etc/x.key"), "pilot")


@pytest.mark.parametrize("env, match", [
    ({"HAPILOT_UI_HOST": "intranet.example.com"}, "HAPILOT_UI_HOST"),
    ({"HAPILOT_UI_SSL_CERT": "/etc/x.crt"}, "together"),
    ({"HAPILOT_UI_SSL_KEY": "/etc/x.key"}, "together"),
])
def test_bad_ui_settings_are_rejected(env, match):
    with pytest.raises(ConfigError, match=match):
        load_settings(env)


def test_unit_tests_do_not_read_the_hosts_dotenv(tmp_path, monkeypatch):
    """A running install's .env (its data folder, documents, accounts) must never reach the unit tests."""
    import src.config as config

    (tmp_path / ".env").write_text("HAPILOT_UI_PORT=9801\n")
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("HAPILOT_UI_PORT", raising=False)
    assert config.load_settings().ui_port == 8601
    monkeypatch.delenv("HAPILOT_UI_PORT", raising=False)


# --------------------------------------------------------------------------- UI command and address

def _settings(monkeypatch, **env):
    from src.config import reset_settings

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    reset_settings()


def test_ui_command_uses_the_configured_address_and_certificate(tmp_path, monkeypatch):
    cert, key = tmp_path / "ui.crt", tmp_path / "ui.key"
    _settings(monkeypatch, HAPILOT_UI_HOST="0.0.0.0", HAPILOT_UI_SSL_CERT=str(cert), HAPILOT_UI_SSL_KEY=str(key))
    with pytest.raises(SystemExit, match="not found"):
        hapilotctl.ui_command()
    cert.write_text("c")
    key.write_text("k")
    cmd = hapilotctl.ui_command()
    assert cmd[cmd.index("--host") + 1] == "0.0.0.0"
    assert cmd[cmd.index("--ssl-cert") + 1] == str(cert) and cmd[cmd.index("--ssl-key") + 1] == str(key)
    assert "from the local network: https://<this machine's address>:" in hapilotctl.ui_address()
    assert hapilotctl.tunnel_command().startswith(f"ssh -N -L {hapilotctl.UI_PORT}:127.0.0.1:")


def test_default_ui_command_stays_on_localhost(monkeypatch):
    _settings(monkeypatch)
    cmd = hapilotctl.ui_command()
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1" and "--ssl-cert" not in cmd
    assert hapilotctl.ui_address().startswith("through an SSH tunnel")
    _settings(monkeypatch, HAPILOT_UI_HOST="192.0.2.10")
    assert hapilotctl.tunnel_command().startswith(f"ssh -N -L {hapilotctl.UI_PORT}:192.0.2.10:")
    assert hapilotctl.ui_address().endswith(f"http://192.0.2.10:{hapilotctl.UI_PORT}")


def test_local_url():
    assert hapilotctl.local_url("0.0.0.0", 8641, True, "/auth/config") == "https://127.0.0.1:8641/auth/config"
    assert hapilotctl.local_url("192.0.2.10", 8641) == "http://192.0.2.10:8641"
    assert hapilotctl.local_url("::", 8641) == "http://[::1]:8641"


def test_ensure_leaves_a_ui_stopped_by_the_operator_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(hapilotctl, "run_dir", lambda: tmp_path)
    started = []
    monkeypatch.setattr(hapilotctl, "ui_up", lambda profile=None: started.append(profile))
    monkeypatch.setattr(hapilotctl, "stop", lambda name: None)
    hapilotctl.ui_ensure()
    assert started == [None]  # not running, not stopped on purpose → started
    hapilotctl.ui_down()
    hapilotctl.ui_ensure()
    assert started == [None] and hapilotctl.ui_stopped_marker().is_file()


def test_make_cert(tmp_path):
    if not __import__("shutil").which("openssl"):
        pytest.skip("openssl not installed")
    import argparse

    hapilotctl.cmd_make_cert(argparse.Namespace(out=tmp_path / "tls", ip=["192.0.2.10"], dns=["hapilot.test"], days=30,
                                                force=False))
    cert, key = tmp_path / "tls" / "hapilot.crt", tmp_path / "tls" / "hapilot.key"
    text = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-text"], capture_output=True, text=True).stdout
    assert "IP Address:192.0.2.10" in text and "DNS:hapilot.test" in text and "DNS:localhost" in text
    assert key.stat().st_mode & 0o077 == 0
    with pytest.raises(SystemExit, match="exists"):
        hapilotctl.cmd_make_cert(argparse.Namespace(out=tmp_path / "tls", ip=[], dns=[], days=30, force=False))


# --------------------------------------------------------------------------- activity file

def test_tracked_handlers_count_while_they_run(tmp_path, monkeypatch):
    monkeypatch.setattr(ui_activity, "status_file", lambda: tmp_path / "ui_activity.json")
    seen = []

    @ui_activity.tracked
    async def handler(message):
        seen.append(json.loads((tmp_path / "ui_activity.json").read_text())["active"])
        return message.upper()

    assert asyncio.run(handler(message="tak")) == "TAK"
    info = json.loads((tmp_path / "ui_activity.json").read_text())
    assert seen == [1] and info["active"] == 0 and info["pid"] == __import__("os").getpid()
    import inspect

    assert list(inspect.signature(handler).parameters) == ["message"]  # Chainlit passes arguments by these names


def test_idle_rules(tmp_path):
    path = tmp_path / "ui_activity.json"
    assert ui_activity.idle(123, 600, path)[0] is True  # nothing recorded
    path.write_text(json.dumps({"pid": 123, "active": 1, "updated": 1000.0}))
    assert ui_activity.idle(123, 600, path, now=99999)[0] is False  # an answer is running
    assert ui_activity.idle(456, 600, path, now=99999)[0] is True  # file of an earlier UI process
    path.write_text(json.dumps({"pid": 123, "active": 0, "updated": 1000.0}))
    assert ui_activity.idle(123, 600, path, now=1300)[0] is False  # 5 min ago
    assert ui_activity.idle(123, 600, path, now=1700)[0] is True  # 11.7 min ago


# --------------------------------------------------------------------------- updater

def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout.strip()


def _commit(work: Path, files: dict, message: str) -> str:
    for name, text in files.items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(text)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", message)
    _git(work, "push", "-q", "origin", "HEAD:main")
    return _git(work, "rev-parse", "HEAD")


@pytest.fixture
def install(tmp_path, monkeypatch):
    """An install (clone of a bare 'GitHub') with a fake UI, and a developer clone that pushes new commits."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    dev = tmp_path / "dev"
    _git(tmp_path, "clone", "-q", str(origin), str(dev))
    _git(dev, "checkout", "-q", "-b", "main")
    first = _commit(dev, {"tests/test_ok.py": "def test_ok():\n    assert True\n"}, "first")
    live = tmp_path / "live"
    _git(tmp_path, "clone", "-q", str(origin), str(live))
    var = tmp_path / "var"
    for name in ("run", "log"):
        (var / name).mkdir(parents=True)
    db = tmp_path / "chat.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE threads (id TEXT)")
        conn.execute("INSERT INTO threads VALUES ('chat-1')")
    monkeypatch.setenv("HAPILOT_CHAT_DB", str(db))
    monkeypatch.setattr(hapilotctl, "ROOT", live)
    monkeypatch.setattr(hapilotctl, "var_dir", lambda: var)
    monkeypatch.setattr(hapilotctl, "run_dir", lambda: var / "run")
    monkeypatch.setattr(hapilotctl, "log_dir", lambda: var / "log")
    events = []
    ui = {"running": True, "healthy": True}
    monkeypatch.setattr(hapilotctl, "owned", lambda name: {"pid": 4242} if name == "ui" and ui["running"] else None)
    monkeypatch.setattr(hapilotctl, "switch_services", lambda ui_running, force_ingest=False: events.append(
        ("switch", _git(live, "rev-parse", "HEAD"), ui_running, force_ingest)))

    def health():
        if not ui["healthy"]:
            raise hapilotctl.UpdateError("the UI does not answer: refused")

    monkeypatch.setattr(hapilotctl, "ui_health", health)
    monkeypatch.setattr(ui_activity, "status_file", lambda: var / "run" / "ui_activity.json")
    return {"dev": dev, "live": live, "var": var, "first": first, "events": events, "ui": ui}


def _head(install):
    return _git(install["live"], "rev-parse", "HEAD")


def test_update_without_new_commits_changes_nothing(install):
    assert hapilotctl.update("main") == 0
    assert _head(install) == install["first"] and install["events"] == []
    assert hapilotctl.read_update_status()["state"] == "current"
    assert not (install["var"] / "backups").exists()


def test_update_tests_switches_restarts_and_backs_up(install):
    new = _commit(install["dev"], {"tests/test_more.py": "def test_more():\n    assert 2 + 2 == 4\n"}, "second")
    assert hapilotctl.update("main") == 0
    assert _head(install) == new
    assert install["events"] == [("switch", new, True, False)]  # after the switch, with the new commit checked out
    status = hapilotctl.read_update_status()
    assert status["state"] == "updated" and "2 passed" in status["message"]
    backup = Path(status["last_update"]["backup"])
    with sqlite3.connect(backup / "chat_history.sqlite") as conn:
        assert conn.execute("SELECT id FROM threads").fetchall() == [("chat-1",)]
    assert "updated" in (install["var"] / "log" / "update.log").read_text()
    assert not (install["var"] / "update" / "worktree").exists()


def test_failing_tests_keep_the_running_version_and_the_commit_is_not_retried(install):
    bad = _commit(install["dev"], {"tests/test_bad.py": "def test_bad():\n    assert False\n"}, "broken")
    assert hapilotctl.update("main") == 1
    assert _head(install) == install["first"] and install["events"] == []  # never switched, UI untouched
    status = hapilotctl.read_update_status()
    assert status["state"] == "rolled_back" and status["failed_target"] == bad and "1 failed" in status["message"]
    assert (install["var"] / "log" / "update-tests.log").is_file()
    assert hapilotctl.update("main") == 0 and hapilotctl.read_update_status()["state"] == "skipped"
    fixed = _commit(install["dev"], {"tests/test_bad.py": "def test_bad():\n    assert True\n"}, "fixed")
    assert hapilotctl.update("main") == 0 and _head(install) == fixed
    assert hapilotctl.read_update_status()["failed_target"] is None


def test_ui_that_does_not_come_back_is_rolled_back(install):
    new = _commit(install["dev"], {"README": "new\n"}, "second")
    install["ui"]["healthy"] = False
    assert hapilotctl.update("main") == 1
    assert _head(install) == install["first"]
    # the old commit rebuilds the index (the new one may have changed it) and starts its UI
    assert install["events"] == [("switch", new, True, False), ("switch", install["first"], True, True)]
    status = hapilotctl.read_update_status()
    assert status["state"] == "error"  # the old version did not answer either (the fake UI stays down)
    install["ui"]["healthy"] = True
    assert hapilotctl.update("main", retry=True) == 0 and _head(install) == new


def test_backups_of_updates_in_the_same_second_do_not_collide(install):
    first, second = hapilotctl.backup_data("a" * 40), hapilotctl.backup_data("a" * 40)
    assert first != second and first.is_dir() and second.is_dir()  # failed on a fast machine: FileExistsError


def test_update_waits_while_people_use_the_ui(install):
    _commit(install["dev"], {"README": "new\n"}, "second")
    (install["var"] / "run" / "ui_activity.json").write_text(json.dumps({"pid": 4242, "active": 1, "updated": 0}))
    assert hapilotctl.update("main") == 0
    assert _head(install) == install["first"] and hapilotctl.read_update_status()["state"] == "deferred"
    assert hapilotctl.update("main", now=True) == 0 and _head(install) != install["first"]


def test_update_without_a_running_ui_does_not_start_it(install):
    install["ui"]["running"] = False
    new = _commit(install["dev"], {"README": "new\n"}, "second")
    assert hapilotctl.update("main") == 0
    assert _head(install) == new and install["events"] == [("switch", new, False, False)]  # index only, no UI start


def test_hand_edited_checkout_is_not_updated(install):
    _commit(install["dev"], {"README": "new\n"}, "second")
    (install["live"] / "tests" / "test_ok.py").write_text("# edited by hand\n")
    assert hapilotctl.update("main") == 1
    assert _head(install) == install["first"] and hapilotctl.read_update_status()["state"] == "error"


def test_new_commit_builds_its_own_images_without_the_installs_settings(install, monkeypatch):
    """A commit with the OCR image's Dockerfile builds its missing images with its own launcher; the install's
    settings (HAPILOT_*) do not reach it."""
    marker = install["var"] / "built.txt"
    monkeypatch.setenv("HAPILOT_UI_HOST", "0.0.0.0")
    monkeypatch.setenv("BUILD_MARKER", str(marker))
    script = ("import os, sys\n"
              "open(os.environ['BUILD_MARKER'], 'w').write(' '.join(sys.argv[1:]) + ' ' + "
              "str(sorted(k for k in os.environ if k.startswith('HAPILOT_'))))\n")
    new = _commit(install["dev"], {"sandbox/ocr.Dockerfile": "FROM scratch\n", "scripts/hapilotctl.py": script}, "ocr")
    assert hapilotctl.update("main") == 0 and _head(install) == new
    assert marker.read_text() == "build-images --quiet []"


def test_switch_services_stops_indexes_and_starts(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(hapilotctl, "log_dir", lambda: tmp_path)
    monkeypatch.setattr(hapilotctl, "stop", lambda name: calls.append(("stop", name)))

    class Done:
        def __init__(self, code=0):
            self.returncode, self.stdout, self.stderr = code, "out", "boom" if code else ""

    fail = {"step": None}

    def run(cmd, **kwargs):
        calls.append(tuple(cmd[2:]))
        return Done(1 if cmd[2] == fail["step"] else 0)

    monkeypatch.setattr(hapilotctl.subprocess, "run", run)
    hapilotctl.switch_services(True)
    assert calls == [("stop", "ui"), ("ingest",), ("ui", "up")]
    calls.clear()
    hapilotctl.switch_services(False, force_ingest=True)
    assert calls == [("ingest", "--force")]
    calls.clear()
    fail["step"] = "ingest"
    with pytest.raises(hapilotctl.UpdateError, match="indexing failed: boom"):
        hapilotctl.switch_services(True)
    assert calls == [("stop", "ui"), ("ingest",)]  # the UI is not started on a half-built index


def test_existing_image_tags_are_never_rebuilt(monkeypatch):
    calls = []

    class Done:
        def __init__(self, code):
            self.returncode = code

    def run(cmd, **kwargs):
        calls.append(cmd[:3])
        return Done(0 if cmd[:3] == ["docker", "image", "inspect"] and cmd[3] == "hapilot-sandbox:0.2" else 1
                    if cmd[:3] == ["docker", "image", "inspect"] else 0)

    monkeypatch.setattr(hapilotctl.subprocess, "run", run)
    hapilotctl.build_images(quiet=True)
    assert calls == [["docker", "image", "inspect"], ["docker", "image", "inspect"], ["docker", "build", "-q"]]


def test_images_include_the_ocr_image():
    tags = [tag for tag, _ in hapilotctl.images()]
    assert tags == ["hapilot-sandbox:0.2", "hapilot-ocr:0.1"]
    assert all(dockerfile.is_file() for _, dockerfile in hapilotctl.images())
