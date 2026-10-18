"""Isolated execution of generated Python code and LaTeX compilation.

Every run is a fresh, short-lived Docker container:
- no network (``network_mode="none"``), no Docker socket, all capabilities dropped,
  ``no-new-privileges``, read-only root filesystem with a small /tmp;
- runs as the invoking (non-root) user, with memory, CPU and process limits;
- sees only the current session's workspace, mounted at /work;
- is killed when it exceeds its timeout, and removed afterwards.

The sandbox image is built once by ``scripts/hapilotctl.py setup``; it is never pulled at run time.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from src.config import get_settings


class SandboxUnavailable(RuntimeError):
    """Docker or the sandbox image is not available."""


@dataclass
class SandboxResult:
    exit_code: int
    output: str
    timed_out: bool
    network_mode: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


_client: Any = None
history: list[dict] = []  # one entry per sandbox run in this process (used by the egress test report)


def docker_client() -> Any:
    """Lazily created Docker client (tests replace it with ``set_docker_client``)."""
    global _client
    if _client is None:
        try:
            import docker

            _client = docker.from_env()
        except Exception as exc:  # docker not installed / daemon not reachable
            raise SandboxUnavailable(f"Docker is not available: {exc}") from exc
    return _client


def set_docker_client(client: Any) -> None:
    global _client
    _client = client


def _user() -> str:
    uid = os.getuid() if hasattr(os, "getuid") else 1000
    gid = os.getgid() if hasattr(os, "getgid") else 1000
    if uid == 0:  # never run sandboxed code as root, even if Hapilot itself is
        uid, gid = 65534, 65534
    return f"{uid}:{gid}"


def container_options(workdir: Path) -> dict:
    """Keyword arguments for ``containers.run``; kept separate so tests can check them."""
    s = get_settings()
    return {
        "detach": True,
        "network_mode": "none",
        "user": _user(),
        "working_dir": "/work",
        "volumes": {str(workdir.resolve()): {"bind": "/work", "mode": "rw"}},
        "read_only": True,
        "tmpfs": {"/tmp": "rw,size=256m,mode=1777"},
        "mem_limit": s.sandbox_memory,
        "memswap_limit": s.sandbox_memory,
        "nano_cpus": int(s.sandbox_cpus * 1e9),
        "pids_limit": s.sandbox_pids,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "environment": {"HOME": "/tmp", "MPLCONFIGDIR": "/tmp", "TEXMFVAR": "/tmp/texmf-var",
                        "PYTHONDONTWRITEBYTECODE": "1"},
        "labels": {"app": "hapilot", "role": "sandbox"},
    }


def run(command: list[str], workdir: Path, timeout: Optional[int] = None) -> SandboxResult:
    """Run ``command`` in a fresh sandbox container with ``workdir`` mounted at /work."""
    s = get_settings()
    client = docker_client()
    try:
        client.images.get(s.sandbox_image)
    except Exception as exc:
        raise SandboxUnavailable(
            f"Sandbox image {s.sandbox_image!r} not found — build it with `scripts/hapilotctl.py setup`"
        ) from exc

    container = client.containers.run(s.sandbox_image, command, **container_options(workdir))
    timed_out = False
    try:
        try:
            status = container.wait(timeout=timeout or s.python_timeout)
            exit_code = int(status.get("StatusCode", 1))
        except Exception:  # requests ReadTimeout / ConnectionError while waiting
            timed_out = True
            exit_code = 124
            try:
                container.kill()
            except Exception:
                pass
        output = container.logs(stdout=True, stderr=True).decode("utf-8", errors="replace")
        network_mode = (getattr(container, "attrs", {}) or {}).get("HostConfig", {}).get("NetworkMode", "none")
    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass
    history.append({"command": command[0], "exit_code": exit_code, "timed_out": timed_out, "network_mode": network_mode})
    return SandboxResult(exit_code=exit_code, output=output.strip(), timed_out=timed_out, network_mode=network_mode)
