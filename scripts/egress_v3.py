#!/usr/bin/env python3
"""Egress test of the v3 agent container (the v2 test, `hapilotctl.py egress-test`, covers the host processes):

    .venv/bin/python scripts/egress_v3.py [--profile best-local] [--report FILE]

1. The agent network is a Docker *internal* network (no route out of the host).
2. A probe in a container started with exactly the agent's flags (image, network, user, read-only root, no
   capabilities, the same mounts) tries to get out: a public DNS name, public addresses (TCP and UDP), the Docker host
   directly, bypassing the relay. Every attempt must fail. The model relay `model:8000` must answer and serve the
   profile's model (this is also how the integration suite checks that the model is up).
3. The runner's own guard (agent/runner/egress_guard.mjs, loaded before the SDK) refuses and logs a connection from
   Node to anything but the allowed host, even where the network would carry it.

Writes a JSON report; exit code 0 when every check passed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROBE = r'''
import json, socket, sys, urllib.request
model_host, model_port = sys.argv[1], int(sys.argv[2])
out = {}

def tcp(host, port):
    try:
        with socket.create_connection((host, port), timeout=4):
            return "connected"
    except OSError as exc:
        return f"blocked ({type(exc).__name__})"

try:
    socket.getaddrinfo("example.com", 443)
    out["dns example.com"] = "resolved"
except OSError as exc:
    out["dns example.com"] = f"blocked ({type(exc).__name__})"
for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 443), ("172.17.0.1", model_port), ("172.17.0.1", 22)):
    out[f"tcp {host}:{port}"] = tcp(host, port)
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    s.sendto(b"\x00" * 12, ("8.8.8.8", 53))
    s.recvfrom(512)
    out["udp 8.8.8.8:53"] = "answered"
except OSError as exc:
    out["udp 8.8.8.8:53"] = f"blocked ({type(exc).__name__})"
try:
    with urllib.request.urlopen(f"http://{model_host}:{model_port}/v1/models", timeout=10) as r:
        out["model"] = [m.get("id") for m in json.loads(r.read()).get("data", [])]
except Exception as exc:
    out["model"] = f"unreachable ({type(exc).__name__})"
print(json.dumps(out))
'''

NODE_PROBE = r'''
const r = await fetch("http://1.1.1.1/").then(() => "connected", (e) => "blocked: " + (e.cause?.message ?? e.message));
console.log(JSON.stringify({ node_fetch: r }));
'''


def probe_command(settings, image_cmd: list[str]) -> list[str]:
    """The agent's own `docker run` (src/agent_engine.py) with the runner replaced by ``image_cmd``."""
    from src.agent_engine import AgentEngine
    from src.agent_workspace import Workspace

    work = Path(tempfile.mkdtemp(prefix="hapilot-egress-"))
    ws = Workspace("egress-probe", root=work, settings=settings).prepare()
    for name in ("run", "dom", "sock"):
        (work / name).mkdir()
    cmd = AgentEngine(settings).container_command(ws, work / "run", work / "dom", work / "sock",
                                                  f"hapilot-v3-egress-{int(time.time())}")
    image_at = cmd.index(settings.agent_image)
    return cmd[:image_at + 1] + image_cmd, work


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", help="agent profile (config/agent_profiles.yaml)")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)

    import dataclasses

    from src.config import get_settings
    from src.eval.agent import agent_model, image_id

    profile, model = agent_model(args.profile)
    settings = dataclasses.replace(get_settings(), agent_model=model, agent_email=False)
    host, _, port = settings.agent_allow.partition(":")
    checks: list[dict] = []

    def record(name: str, ok: bool, detail) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}", flush=True)

    net = subprocess.run(["docker", "network", "inspect", "--format", "{{.Internal}}", settings.agent_network],
                         capture_output=True, text=True)
    record("agent network is internal", net.stdout.strip() == "true", net.stdout.strip() or net.stderr.strip()[:200])

    cmd, work = probe_command(settings, ["python3", "-c", PROBE, host, port or "8000"])
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        result = json.loads(done.stdout.strip().splitlines()[-1]) if done.stdout.strip() else {}
        if not result:
            record("probe ran", False, done.stderr.strip()[-300:])
        for name, value in result.items():
            if name == "model":
                served = isinstance(value, list) and model in value
                record(f"model relay {settings.agent_allow} serves {model}", served, value)
            else:
                record(f"no way out: {name}", str(value).startswith("blocked"), value)

        node_cmd, node_work = probe_command(settings, ["node", "--import", "/opt/hapilot-agent/egress_guard.mjs",
                                                       "--input-type=module", "-e", NODE_PROBE])
        try:
            node = subprocess.run(node_cmd, capture_output=True, text=True, timeout=120)
            line = node.stdout.strip().splitlines()[-1] if node.stdout.strip() else "{}"
            verdict = json.loads(line).get("node_fetch", node.stderr.strip()[-200:])
            logged = (node_work / "run" / "egress.jsonl")
            entries = [json.loads(x) for x in logged.read_text().splitlines()] if logged.is_file() else []
            record("runner guard refuses Node connections", str(verdict).startswith("blocked"), verdict)
            record("runner guard logs the refusal", any(not e.get("allowed", True) for e in entries),
                   [f"{e.get('host')}:{e.get('port')} allowed={e.get('allowed')}" for e in entries])
        finally:
            shutil.rmtree(node_work, ignore_errors=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    report = {"engine": "v3", "profile": profile, "model": model, "image": settings.agent_image,
              "image_id": image_id(settings.agent_image), "network": settings.agent_network,
              "allowed": settings.agent_allow, "checks": checks, "passed": bool(checks) and all(c["ok"] for c in checks)}
    out = args.report or settings.data_dir / "egress" / f"egress_v3_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"egress v3 {'PASSED' if report['passed'] else 'FAILED'} — {out}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
