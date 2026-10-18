"""Keep Hapilot's own process off the internet.

``apply_offline_env()`` switches off telemetry and runtime downloads of the libraries Hapilot uses
(Hugging Face, Chroma, LangSmith tracing) — call it before those libraries are imported.

``install_egress_guard()`` patches the socket layer so that any connection or DNS lookup to a
non-local address raises ``EgressBlocked`` and is recorded. The egress test
(``scripts/hapilotctl.py egress-test``) runs the scenario suite with the guard on and, in addition,
traces the process tree with strace; one outbound attempt fails the test.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Optional

OFFLINE_ENV = {
    # Hugging Face: no downloads, no telemetry (the embedding model is fetched once by `setup`)
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    # Chroma: no anonymised product telemetry
    "ANONYMIZED_TELEMETRY": "False",
    # LangChain / LangSmith: no tracing to the cloud
    "LANGCHAIN_TRACING_V2": "false",
    "LANGSMITH_TRACING": "false",
    # Generic opt-outs honoured by several tools (vLLM usage stats among them)
    "DO_NOT_TRACK": "1",
    "VLLM_NO_USAGE_STATS": "1",
}


def apply_offline_env(allow_downloads: bool = False) -> None:
    """Set the offline/telemetry environment for this process (and its children)."""
    for key, value in OFFLINE_ENV.items():
        if allow_downloads and key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
            os.environ[key] = "0"
            continue
        os.environ[key] = value


class EgressBlocked(PermissionError):
    """Raised instead of opening a connection to a non-local address."""


_ATTEMPTS: list[dict] = []
_LOCK = threading.Lock()
_INSTALLED = False
_LOG_PATH: Optional[Path] = None


def _is_local_host(host: Any) -> bool:
    if host is None:
        return True
    host = str(host).strip("[]").lower()
    if host in ("", "localhost") or host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False  # a DNS name other than localhost
    return ip.is_loopback or ip.is_private or ip.is_unspecified


def _record(kind: str, target: Any) -> None:
    entry = {"time": time.time(), "kind": kind, "target": repr(target)[:200],
             "stack": "".join(traceback.format_stack(limit=8)[:-2])[-2000:]}
    with _LOCK:
        _ATTEMPTS.append(entry)
        if _LOG_PATH is not None:
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def attempts() -> list[dict]:
    with _LOCK:
        return list(_ATTEMPTS)


def install_egress_guard(log_path: Optional[Path] = None) -> None:
    """Block and record every non-local connection attempt made through Python sockets."""
    global _INSTALLED, _LOG_PATH
    _LOG_PATH = log_path
    if _INSTALLED:
        return
    _INSTALLED = True

    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex
    orig_getaddrinfo = socket.getaddrinfo

    def _check(sock: socket.socket, address: Any) -> None:
        if sock.family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple):
            if not _is_local_host(address[0]):
                _record("connect", address)
                raise EgressBlocked(f"Hapilot egress guard: connection to {address[0]} blocked")

    def connect(self, address):  # type: ignore[no-untyped-def]
        _check(self, address)
        return orig_connect(self, address)

    def connect_ex(self, address):  # type: ignore[no-untyped-def]
        _check(self, address)
        return orig_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if not _is_local_host(host if not isinstance(host, bytes) else host.decode()):
            _record("dns", host)
            raise EgressBlocked(f"Hapilot egress guard: DNS lookup of {host!r} blocked")
        return orig_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
