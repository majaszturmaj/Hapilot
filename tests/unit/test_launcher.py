"""The launcher stops only its own processes and the egress test recognises outbound connections."""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import hapilotctl  # noqa: E402

STRACE = """\
123 connect(5, {sa_family=AF_INET, sin_port=htons(8611), sin_addr=inet_addr("127.0.0.1")}, 16) = 0
124 connect(6, {sa_family=AF_INET, sin_port=htons(443), sin_addr=inet_addr("140.82.112.3")}, 16) = -1 EINPROGRESS
125 connect(7, {sa_family=AF_INET, sin_port=htons(53), sin_addr=inet_addr("127.0.0.53")}, 16) = 0
126 connect(8, {sa_family=AF_INET6, sin6_port=htons(443), sin6_flowinfo=htonl(0), inet_pton(AF_INET6, "2606:4700::1", &sin6_addr), sin6_scope_id=0}, 28) = -1
127 connect(9, {sa_family=AF_UNIX, sun_path="/var/run/docker.sock"}, 23) = 0
128 connect(10, {sa_family=AF_INET, sin_port=htons(8000), sin_addr=inet_addr("192.168.1.10")}, 16) = 0
"""


def test_strace_violations(tmp_path):
    log = tmp_path / "strace.log"
    log.write_text(STRACE)
    bad = hapilotctl.strace_violations(log)
    assert [line.split()[0] for line in bad] == ["124", "125", "126"]  # external IPv4, DNS, external IPv6


def test_stale_or_reused_pid_record_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(hapilotctl, "run_dir", lambda: tmp_path)
    # a record pointing at this test process, whose command line does not contain the marker (a unique one: the
    # test runner's own command line may name src/app.py, e.g. in a coverage --omit option)
    marker = f"src/app-{__import__('uuid').uuid4().hex}.py"
    (tmp_path / "ui.json").write_text(json.dumps({"pid": __import__("os").getpid(), "pgid": 0, "marker": marker}))
    assert hapilotctl.owned("ui") is None
    assert not (tmp_path / "ui.json").exists()


def test_status_files_in_the_run_folder_are_not_process_records(tmp_path, monkeypatch):
    """update.json and ui_activity.json sit next to the process records; `status` (and the GPU check before starting
    models) crashed with KeyError: 'pid' on them."""
    import os

    monkeypatch.setattr(hapilotctl, "run_dir", lambda: tmp_path)
    monkeypatch.setattr(hapilotctl, "gpu_users", lambda: [])
    (tmp_path / "update.json").write_text(json.dumps({"state": "current", "time": "t", "commit": "a" * 40, "message": "m"}))
    (tmp_path / "ui_activity.json").write_text(json.dumps({"pid": os.getpid(), "active": 0, "updated": 0}))
    assert hapilotctl.our_pids() == set()
    hapilotctl.cmd_status(None)
    assert (tmp_path / "update.json").is_file() and (tmp_path / "ui_activity.json").is_file()


def test_launcher_has_no_broad_kill_commands():
    text = (ROOT / "scripts" / "hapilotctl.py").read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    code = re.sub(r'"""[\s\S]*?"""', "", code)
    code = code.replace('git("worktree", "prune")', "")  # git's bookkeeping of the updater's own temporary checkout
    for forbidden in ("pkill", "killall", "docker rm", "prune", "remove-orphans", "down --"):
        assert forbidden not in code, forbidden


def test_proxy_binds_localhost_only():
    text = (ROOT / "tools" / "modelswap.py").read_text()
    assert 'ThreadingHTTPServer(("127.0.0.1"' in text and '"--host", "127.0.0.1"' in text


def test_port_free_detects_listeners_only():
    import socket

    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    assert hapilotctl.port_free(port) is False
    client = socket.create_connection(("127.0.0.1", port))
    conn, _ = server.accept()
    conn.close()  # server side closes first → TIME_WAIT on the listening port
    client.close()
    server.close()
    assert hapilotctl.port_free(port) is True
