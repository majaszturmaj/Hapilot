"""The model swap proxy routes by model name, serialises swaps and listens on localhost only (stub backend)."""

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import modelswap  # noqa: E402


class StubBackend:
    def __init__(self):
        self.config = {"models": {"a": {}, "b": {}}}
        self.model = None
        self.loads = []
        self.lock = threading.Lock()

    def ensure(self, name):
        if name not in self.config["models"]:
            raise KeyError(f"unknown model {name!r}")
        if name != self.model:
            self.loads.append(name)
            self.model = name

    def stop(self):
        self.model = None

    def forward(self, method, path, body):
        payload = json.loads(body)
        reply = {"choices": [{"message": {"content": f"{self.model}:{payload['messages'][0]['content']}"}}]}
        return 200, "application/json", json.dumps(reply).encode()


def _post(port, path, payload):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def test_routing_and_swapping():
    backend = StubBackend()
    server = ThreadingHTTPServer(("127.0.0.1", 0), modelswap.make_handler(backend))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=5) as resp:
            assert [m["id"] for m in json.loads(resp.read())["data"]] == ["a", "b"]
        chat = {"messages": [{"role": "user", "content": "x"}]}
        assert _post(port, "/v1/chat/completions", {**chat, "model": "a"})["choices"][0]["message"]["content"] == "a:x"
        assert _post(port, "/v1/chat/completions", {**chat, "model": "a"})["choices"][0]["message"]["content"] == "a:x"
        assert _post(port, "/v1/chat/completions", {**chat, "model": "b"})["choices"][0]["message"]["content"] == "b:x"
        assert backend.loads == ["a", "b"]  # one load per change of model
        try:
            _post(port, "/v1/chat/completions", {**chat, "model": "nope"})
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
            assert json.loads(resp.read())["loaded"] == "b"
    finally:
        server.shutdown()


def test_config_paths_are_expanded(tmp_path):
    cfg = tmp_path / "models.yaml"
    cfg.write_text("llama_server: ~/bin/llama-server\nmodels:\n  m:\n    gguf: ~/models/m.gguf\n    args: [--reasoning-budget, 0]\n")
    loaded = modelswap.load_config(cfg)
    assert str(loaded["models"]["m"]["gguf"]).startswith(str(Path.home()))
    assert loaded["models"]["m"]["args"] == ["--reasoning-budget", "0"] and loaded["models"]["m"]["ctx"] == 32768
