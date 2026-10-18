#!/usr/bin/env python3
"""Model swap proxy: one OpenAI-compatible endpoint for several local GGUF models on one GPU.

A request names its model (``"model": "bielik-11b-v3-q8"``). If that model is not loaded, the proxy
stops the llama.cpp server it started for the previous model and starts one for the requested model,
then forwards the request. Requests are handled one at a time. With a single-model profile no swap
ever happens. Everything listens on 127.0.0.1 only.

    python tools/modelswap.py --config config/models.local.yaml --port 8611 --backend-port 8612

Endpoints: GET /health, GET /v1/models, POST /v1/chat/completions, POST /v1/completions,
POST /admin/load {"model": name} (preload), POST /admin/unload.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

LLAMA_COMMON = ["-ngl", "999", "-np", "1", "--flash-attn", "on", "--cache-type-k", "q8_0", "--cache-type-v", "q8_0",
                "--jinja", "--seed", "42"]


def load_config(path: Path) -> dict:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    server = Path(os.path.expanduser(raw.get("llama_server", ""))).resolve()
    models = {}
    for name, m in (raw.get("models") or {}).items():
        models[name] = {"gguf": Path(os.path.expanduser(m["gguf"])), "ctx": int(m.get("ctx", 32768)),
                        "args": [str(a) for a in m.get("args", [])]}
    return {"llama_server": server, "models": models}


class Backend:
    """The single llama.cpp server process started by this proxy."""

    def __init__(self, config: dict, port: int, log_dir: Path, ready_timeout: float):
        self.config, self.port, self.log_dir, self.ready_timeout = config, port, log_dir, ready_timeout
        self.proc: subprocess.Popen | None = None
        self.model: str | None = None
        self.lock = threading.Lock()

    def _healthy(self) -> bool:
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
            conn.request("GET", "/health")
            ok = conn.getresponse().status == 200
            conn.close()
            return ok
        except OSError:
            return False

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)  # own process group, started below
                self.proc.wait(timeout=30)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.proc, self.model = None, None

    def ensure(self, name: str) -> None:
        if name not in self.config["models"]:
            raise KeyError(f"unknown model {name!r}; configured: {', '.join(self.config['models'])}")
        if self.model == name and self.proc is not None and self.proc.poll() is None:
            return
        self.stop()
        m = self.config["models"][name]
        server = self.config["llama_server"]
        cmd = [str(server), "-m", str(m["gguf"]), "--alias", name, "--host", "127.0.0.1", "--port", str(self.port),
               "-c", str(m["ctx"]), *LLAMA_COMMON, *m["args"]]
        env = {**os.environ, "LD_LIBRARY_PATH": f"{server.parent}:{os.environ.get('LD_LIBRARY_PATH', '')}"}
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log = open(self.log_dir / f"model-{name}.log", "ab")
        print(f"[modelswap] starting {name}", flush=True)
        t0 = time.monotonic()
        self.proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        (self.log_dir / "backend.pid").write_text(str(self.proc.pid))
        while time.monotonic() - t0 < self.ready_timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server for {name} exited with code {self.proc.returncode}; see {log.name}")
            if self._healthy():
                self.model = name
                print(f"[modelswap] {name} ready after {time.monotonic() - t0:.1f}s", flush=True)
                return
            time.sleep(1)
        self.stop()
        raise RuntimeError(f"{name} not ready after {self.ready_timeout:.0f}s")

    def forward(self, method: str, path: str, body: bytes) -> tuple[int, str, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=1800)
        conn.request(method, path, body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read()
        ctype = resp.getheader("Content-Type", "application/json")
        conn.close()
        return resp.status, ctype, data


def make_handler(backend: Backend):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quieter default logging
            sys.stdout.write("[modelswap] " + fmt % args + "\n")

        def _send(self, status: int, payload, ctype: str = "application/json") -> None:
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> bytes:
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"status": "ok", "loaded": backend.model})
            elif self.path in ("/v1/models", "/models"):
                self._send(200, {"object": "list", "data": [{"id": n, "object": "model", "owned_by": "local"}
                                                            for n in backend.config["models"]]})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            body = self._body()
            try:
                payload = json.loads(body or b"{}")
            except json.JSONDecodeError:
                self._send(400, {"error": "invalid JSON"})
                return
            with backend.lock:
                try:
                    if self.path == "/admin/unload":
                        backend.stop()
                        self._send(200, {"status": "unloaded"})
                        return
                    backend.ensure(payload.get("model", ""))
                    if self.path == "/admin/load":
                        self._send(200, {"status": "loaded", "model": backend.model})
                        return
                    if self.path not in ("/v1/chat/completions", "/v1/completions"):
                        self._send(404, {"error": "not found"})
                        return
                    status, ctype, data = backend.forward("POST", self.path, body)
                    self._send(status, data, ctype)
                except KeyError as exc:
                    self._send(404, {"error": str(exc)})
                except Exception as exc:
                    self._send(503, {"error": f"{type(exc).__name__}: {exc}"})

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="Hapilot model swap proxy")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8611)
    parser.add_argument("--backend-port", type=int, default=8612)
    parser.add_argument("--log-dir", type=Path, default=Path("var/log"))
    parser.add_argument("--ready-timeout", type=float, default=600)
    args = parser.parse_args()

    config = load_config(args.config)
    missing = [n for n, m in config["models"].items() if not m["gguf"].is_file()]
    if not config["llama_server"].is_file() or missing:
        print(f"[modelswap] missing llama-server or model files: {missing}", file=sys.stderr)
        return 2
    backend = Backend(config, args.backend_port, args.log_dir, args.ready_timeout)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(backend))

    def shutdown(signum, frame):
        backend.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"[modelswap] listening on 127.0.0.1:{args.port}; models: {', '.join(config['models'])}", flush=True)
    try:
        server.serve_forever()
    finally:
        backend.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
