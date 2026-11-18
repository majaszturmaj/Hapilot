#!/usr/bin/env python3
"""Hapilot v3, stage 2: run prompts through the agent container with Hapilot's domain tools.

Unlike spike.py (benchmark tasks on text copies), this uses the product layout of stage 1: a per-conversation
workspace (src/agent_workspace.py: documents, added documents and the text mirror read-only under /praca), and the
host bridge (src/agent_bridge.py) on a Unix socket mounted read-only at /hapilot. The runner starts the MCP server
agent/mcp/hapilot_mcp.py inside the container.

Run it with Hapilot's Python environment (the bridge searches the index and compiles PDFs in the v2 sandbox), with
HAPILOT_DATA_DIR / HAPILOT_DOCS_DIR pointing at the knowledge base to use:

  tools_run.py --prompts prompts.json --out wyniki/ [--email --approve allow|deny --fake-n8n] [--model ...]

prompts.json: [{"id": "N1", "prompt": "..."}]. Per prompt: out/<id>/{spec.json, trace.jsonl, result.json,
bridge.jsonl, egress.jsonl, egress_proc.jsonl, n8n.jsonl, wyniki.json, stdout.txt, stderr.txt}; out/run.json.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from spike import RUNNER, summarize_trace, watch_container  # noqa: E402

SOCKET_IN_CONTAINER = "/hapilot/most.sock"
GRACE_S = 60


def fake_n8n(log: Path) -> tuple[ThreadingHTTPServer, str]:
    """A stand-in for n8n's draft webhook on 127.0.0.1: records what arrives (field sizes only), answers a draft id."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "path": self.path,
                                     "fields": {k: len(str(v)) for k, v in body.items()}}) + "\n")
            reply = json.dumps({"draft_id": f"test-{int(time.time())}"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/webhook"


def docker_command(a, name: str, ws, run: Path, dom: Path, sock_dir: Path) -> list[str]:
    tools = "semantic_search,pdf_document,working_days" + (",email_draft" if a.email else "")
    return ["docker", "run", "--rm", "--name", name, "--network", a.network,
            "--user", f"{os.getuid()}:{os.getgid()}", "--memory", "16g", "--cpus", "8", "--pids-limit", "1024",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,size=2g",
            "-e", "HOME=/dom", "-e", "DO_NOT_TRACK=1", "-e", "MPLCONFIGDIR=/tmp/mpl",
            "-e", "HAPILOT_EGRESS_LOG=/run/egress.jsonl", "-e", f"HAPILOT_EGRESS_ALLOW={a.allow}",
            "-e", f"HAPILOT_TOOLS={tools}", "-e", f"HAPILOT_BRIDGE={SOCKET_IN_CONTAINER}",
            *ws.docker_args()[:-2],
            "--mount", f"type=bind,source={sock_dir},target=/hapilot,readonly",
            "-v", f"{dom}:/dom", "-v", f"{run}:/run", "--workdir", "/praca", a.image,
            "node", "--import", f"{RUNNER}/egress_guard.mjs", f"{RUNNER}/run_task.mjs", "/run/spec.json"]


def run_prompt(a, item: dict, out: Path, n8n_log: Path) -> dict:
    from src.agent_bridge import Bridge
    from src.agent_workspace import Workspace

    pid = item["id"]
    dest = out / pid
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    ws = Workspace(f"etap2-{pid}", root=Path(a.work).expanduser())
    shutil.rmtree(ws.dir, ignore_errors=True)
    ws.prepare()
    run, dom = ws.dir / "run", ws.dir / "dom"
    run.mkdir()
    dom.mkdir()
    sock_dir = Path(tempfile.mkdtemp(prefix="hb-"))
    spec = {"prompt": item["prompt"], "cwd": "/praca", "providerId": "openai-compatible", "modelId": a.model,
            "apiKey": "lokalny", "baseUrl": a.base_url, "timeoutSeconds": a.timeout,
            "tracePath": "/run/trace.jsonl", "resultPath": "/run/result.json",
            "mcp": {"command": "python3", "args": [f"{RUNNER}/mcp/hapilot_mcp.py"], "timeoutSeconds": 300},
            "approval": {"ask": ["email_draft"], "bridge": SOCKET_IN_CONTAINER}}
    (run / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    approver = (lambda tool, _args: (True, None)) if a.approve == "allow" else None
    bridge = Bridge(ws, email=a.email, approver=approver, trace_path=dest / "bridge.jsonl")
    name = f"hapilot-v3-{pid.lower()}"
    cmd = docker_command(a, name, ws, run, dom, sock_dir)
    stop = threading.Event()
    watcher = threading.Thread(target=watch_container, args=(name, run / "egress_proc.jsonl", stop), daemon=True)
    n8n_before = len(n8n_log.read_text().splitlines()) if n8n_log.is_file() else 0
    t0, status, code = time.time(), "ok", None
    with bridge.serve(sock_dir / "most.sock"):
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        watcher.start()
        try:
            stdout, stderr = proc.communicate(timeout=a.timeout + GRACE_S)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", name], capture_output=True)
            stdout, stderr = proc.communicate()
            status = "timeout"
    stop.set()
    watcher.join(timeout=10)
    shutil.rmtree(sock_dir, ignore_errors=True)
    for f in run.iterdir():
        if f.name != "spec.json" or not (dest / f.name).exists():
            shutil.copy2(f, dest / f.name)
    (dest / "stdout.txt").write_text(stdout or "", encoding="utf-8")
    (dest / "stderr.txt").write_text(stderr or "", encoding="utf-8")
    results = ws.results()
    (dest / "wyniki.json").write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    for r in results:
        if r["name"].endswith(".pdf"):
            (dest / "pdf").mkdir(exist_ok=True)
            shutil.copy2(ws.results_dir / r["name"], dest / "pdf" / Path(r["name"]).name)
    result = json.loads((dest / "result.json").read_text(encoding="utf-8")) if (dest / "result.json").is_file() else {}
    n8n_after = len(n8n_log.read_text().splitlines()) if n8n_log.is_file() else 0
    row = {"id": pid, "status": result.get("status", status), "exit_code": code,
           "duration_s": round(time.time() - t0, 1), "trace": summarize_trace(dest / "trace.jsonl"),
           "bridge_calls": [json.loads(x) for x in (dest / "bridge.jsonl").read_text().splitlines()]
           if (dest / "bridge.jsonl").is_file() else [],
           "results": [r["name"] for r in results], "n8n_drafts": n8n_after - n8n_before,
           "error": (result.get("error") or "")[:300] or None}
    if not a.keep:
        shutil.rmtree(ws.dir, ignore_errors=True)
    return row


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prompts", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", help="comma-separated prompt ids")
    ap.add_argument("--work", default="~/hapilot-v3-data/etap2/praca")
    ap.add_argument("--image", default="hapilot-agent:0.3")
    ap.add_argument("--network", default="hapilot-v3-izolowana")
    ap.add_argument("--allow", default="model:8000")
    ap.add_argument("--model", default="qwen3.6-27b-q4km")
    ap.add_argument("--base-url", default="http://model:8000/v1")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--email", action="store_true", help="enable email_draft for these runs")
    ap.add_argument("--approve", choices=["allow", "deny"], default="deny", help="the approver's fixed answer")
    ap.add_argument("--fake-n8n", action="store_true", help="answer the draft webhook with a local stand-in")
    ap.add_argument("--keep", action="store_true", help="keep the workspaces")
    a = ap.parse_args(argv)
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    n8n_log = out / "n8n.jsonl"
    if a.fake_n8n:
        _server, base = fake_n8n(n8n_log)
        os.environ["HAPILOT_N8N_WEBHOOK_BASE"] = base
    from src.config import get_settings, reset_settings

    reset_settings()
    s = get_settings()
    image_id = subprocess.run(["docker", "image", "inspect", a.image, "--format", "{{.Id}}"],
                              capture_output=True, text=True).stdout.strip()
    meta = {**{k: v for k, v in vars(a).items()}, "image_id": image_id, "docs_dir": str(s.docs_dir),
            "chroma_path": str(s.chroma_path), "collection": s.collection, "start": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (out / "run.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    items = json.loads(Path(a.prompts).expanduser().read_text(encoding="utf-8"))
    if a.only:
        items = [i for i in items if i["id"] in a.only.split(",")]
    rows = []
    for item in items:
        row = run_prompt(a, item, out, n8n_log)
        rows.append(row)
        print(f"[etap2] {row['status']:7} {row['id']:4} {row['duration_s']:6.1f}s tools={row['trace']['tool_calls']} "
              f"bridge={[c['method'] + ('' if c['ok'] else '!') for c in row['bridge_calls']]} "
              f"wyniki={row['results']} n8n={row['n8n_drafts']}", flush=True)
    (out / "results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


if __name__ == "__main__":
    main()
