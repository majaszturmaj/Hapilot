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


# --------------------------------------------------------------------------- engine v3: agent up / down / status

class FakeDocker:
    """Records docker commands; answers inspect calls from a small state (no Docker needed)."""

    def __init__(self, image=True, network=None, relay="running"):
        self.calls, self.image, self.network, self.relay = [], image, network, relay

    def __call__(self, cmd, **kwargs):
        import subprocess

        self.calls.append(cmd)
        ok, out = True, ""
        if cmd[:3] == ["docker", "image", "inspect"]:
            ok = self.image
        elif cmd[:3] == ["docker", "network", "inspect"]:
            ok, out = self.network is not None, self.network or ""
        elif cmd[:2] == ["docker", "inspect"]:
            ok, out = bool(self.relay), self.relay or ""
        return subprocess.CompletedProcess(cmd, 0 if ok else 1, out + "\n" if out else "", "")


def test_agent_up_builds_once_and_starts_an_internal_network_and_relay(monkeypatch):
    from src.config import get_settings

    s = get_settings()
    docker = FakeDocker(image=False, network=None)
    monkeypatch.setattr(hapilotctl.subprocess, "run", docker)
    monkeypatch.setattr(hapilotctl, "upstream_models", lambda: [s.agent_model])
    hapilotctl.agent_up()
    joined = [" ".join(c) for c in docker.calls]
    assert any(c.startswith(f"docker build -t {s.agent_image} -f ") for c in joined)
    assert f"docker network create --internal {s.agent_network}" in joined
    assert f"docker rm -f {s.agent_relay}" in joined  # only our relay, by name
    run = next(c for c in docker.calls if c[:3] == ["docker", "run", "-d"])
    assert run[run.index("--name") + 1] == s.agent_relay and run[run.index("--restart") + 1] == "unless-stopped"
    assert run[-2:] == ["tcp-listen:8000,fork,reuseaddr", f"tcp-connect:{s.agent_upstream}"]
    assert f"docker network connect --alias model {s.agent_network} {s.agent_relay}" in joined


def test_agent_up_reuses_the_image_and_refuses_an_open_network(monkeypatch):
    import pytest

    docker = FakeDocker(image=True, network="false")
    monkeypatch.setattr(hapilotctl.subprocess, "run", docker)
    with pytest.raises(SystemExit, match="not internal"):
        hapilotctl.agent_up()
    assert not any(c[:2] == ["docker", "build"] or c[:2] == ["docker", "run"] for c in docker.calls)


def test_agent_status_needs_every_part(monkeypatch):
    from src.config import get_settings

    model = get_settings().agent_model
    monkeypatch.setattr(hapilotctl, "upstream_models", lambda: [model])
    monkeypatch.setattr(hapilotctl.subprocess, "run", FakeDocker(network="true"))
    assert hapilotctl.agent_status()
    monkeypatch.setattr(hapilotctl.subprocess, "run", FakeDocker(network="true", relay="exited"))
    assert not hapilotctl.agent_status()
    monkeypatch.setattr(hapilotctl.subprocess, "run", FakeDocker(network="true"))
    monkeypatch.setattr(hapilotctl, "upstream_models", lambda: "not answering (refused)")
    assert not hapilotctl.agent_status()


def test_agent_upstream_must_be_local():
    import pytest

    from src.config import NotLocalEndpoint, load_settings

    assert load_settings({"HAPILOT_AGENT_UPSTREAM": "172.17.0.1:8000"}).agent_upstream == "172.17.0.1:8000"
    with pytest.raises(NotLocalEndpoint):
        load_settings({"HAPILOT_AGENT_UPSTREAM": "8.8.8.8:8000"})
