import os
import socket
import threading

import pytest

from src import privacy


def test_offline_env_switches_off_downloads_and_telemetry(monkeypatch):
    for key in privacy.OFFLINE_ENV:
        monkeypatch.delenv(key, raising=False)
    privacy.apply_offline_env()
    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["ANONYMIZED_TELEMETRY"] == "False"
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"
    assert os.environ["VLLM_NO_USAGE_STATS"] == "1"


def test_offline_env_setup_mode_allows_downloads(monkeypatch):
    privacy.apply_offline_env(allow_downloads=True)
    assert os.environ["HF_HUB_OFFLINE"] == "0"
    assert os.environ["ANONYMIZED_TELEMETRY"] == "False"  # telemetry stays off


@pytest.mark.egress_probe
def test_egress_guard_blocks_remote_and_allows_local():
    # the guard is installed for the whole test session by conftest.py
    before = len(privacy.attempts())

    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(privacy.EgressBlocked):
        raw.connect(("93.184.216.34", 80))  # blocked before any packet is sent
    raw.close()
    with pytest.raises(privacy.EgressBlocked):
        socket.getaddrinfo("huggingface.co", 443)
    new = privacy.attempts()[before:]
    assert [a["kind"] for a in new] == ["connect", "dns"]

    # loopback still works
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    threading.Thread(target=lambda: server.accept()[0].close(), daemon=True).start()
    conn = socket.create_connection(("127.0.0.1", port), timeout=2)
    conn.close()
    server.close()
    assert socket.getaddrinfo("localhost", port)
