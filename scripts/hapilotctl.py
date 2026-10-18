#!/usr/bin/env python3
"""Hapilot launcher for the GPU host — starts and stops only what it started itself.

    .venv/bin/python scripts/hapilotctl.py setup            # sandbox image, embedding model, config files
    .venv/bin/python scripts/hapilotctl.py up [--profile P] # model proxy + knowledge base + UI
    .venv/bin/python scripts/hapilotctl.py status | down
    .venv/bin/python scripts/hapilotctl.py smoke [--profile P]
    .venv/bin/python scripts/hapilotctl.py egress-test [--profile P]
    .venv/bin/python scripts/hapilotctl.py add-user NAME | tunnel

Safety rules built in:
- every process it starts is recorded in var/run/<name>.json and stopped by its own process group,
  after checking that the PID still runs the same command (no pkill, no killing by pattern);
- ports are checked before starting; everything binds to 127.0.0.1;
- the model proxy is not started while another process uses the GPU;
- Docker is used only to build the sandbox image (setup) and, from Hapilot itself, for sandbox runs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import get_settings  # noqa: E402

PY = Path(sys.executable)
UI_PORT = int(os.environ.get("HAPILOT_UI_PORT", "8601"))
PROXY_PORT = int(os.environ.get("HAPILOT_MODELSWAP_PORT", "8611"))
BACKEND_PORT = int(os.environ.get("HAPILOT_BACKEND_PORT", "8612"))
MODELS_LOCAL = ROOT / "config" / "models.local.yaml"
EMBEDDING_FILES = ["*.json", "*.txt", "model.safetensors", "sentencepiece.bpe.model", "tokenizer*", "1_Pooling/*"]


def var_dir() -> Path:
    path = get_settings().data_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_dir() -> Path:
    path = var_dir() / "run"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_dir() -> Path:
    path = var_dir() / "log"
    path.mkdir(parents=True, exist_ok=True)
    return path


def say(msg: str) -> None:
    print(f"[hapilotctl] {msg}", flush=True)


# --------------------------------------------------------------------------- processes we own

def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def owned(name: str) -> dict | None:
    """The recorded process if it is still alive and still runs the recorded command."""
    record = run_dir() / f"{name}.json"
    if not record.is_file():
        return None
    info = json.loads(record.read_text())
    if info.get("marker", "") in _cmdline(info["pid"]):
        return info
    record.unlink(missing_ok=True)  # stale record (process gone or PID reused)
    return None


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def start(name: str, cmd: list[str], marker: str, port: int, env: dict | None = None, wait_path: str = "/") -> dict:
    if owned(name):
        say(f"{name} already running")
        return owned(name)  # type: ignore[return-value]
    if not port_free(port):
        raise SystemExit(f"[hapilotctl] port 127.0.0.1:{port} is in use by another process — "
                         f"choose another port (see README, 'Ports').")
    log = open(log_dir() / f"{name}.log", "ab")
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                            env={**os.environ, **(env or {})})
    info = {"name": name, "pid": proc.pid, "pgid": os.getpgid(proc.pid), "marker": marker, "port": port,
            "started": dt.datetime.now().isoformat(timespec="seconds"), "cmd": cmd}
    (run_dir() / f"{name}.json").write_text(json.dumps(info, indent=2))
    for _ in range(180):
        if proc.poll() is not None:
            raise SystemExit(f"[hapilotctl] {name} exited (code {proc.returncode}); see {log.name}")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}{wait_path}", timeout=2)
            say(f"{name} up on 127.0.0.1:{port} (pid {proc.pid})")
            return info
        except Exception:
            time.sleep(1)
    say(f"{name} started (pid {proc.pid}) but 127.0.0.1:{port} does not answer yet; see {log.name}")
    return info


def stop(name: str) -> None:
    info = owned(name)
    if not info:
        say(f"{name} not running")
        return
    try:
        os.killpg(info["pgid"], signal.SIGTERM)
    except ProcessLookupError:
        pass
    for _ in range(30):
        if not _cmdline(info["pid"]):
            break
        time.sleep(1)
    else:
        try:
            os.killpg(info["pgid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
    (run_dir() / f"{name}.json").unlink(missing_ok=True)
    say(f"{name} stopped")


def stop_backend_leftover() -> None:
    """If the proxy died without stopping its llama-server, stop that one (and only that one)."""
    pid_file = log_dir() / "backend.pid"
    if not pid_file.is_file():
        return
    pid = int(pid_file.read_text().strip() or 0)
    cmd = _cmdline(pid)
    if pid and "llama-server" in cmd and f"--port {BACKEND_PORT}" in cmd:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        say(f"stopped leftover llama-server (pid {pid})")
    pid_file.unlink(missing_ok=True)


# --------------------------------------------------------------------------- GPU

def gpu_users() -> list[tuple[int, int]]:
    """(pid, MiB) of processes using the GPU; empty if nvidia-smi is unavailable."""
    if not shutil.which("nvidia-smi"):
        return []
    out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout
    users = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2 and parts[0].isdigit():
            users.append((int(parts[0]), int(parts[1]) if parts[1].isdigit() else 0))
    return users


def our_pids() -> set[int]:
    pids = set()
    for record in run_dir().glob("*.json"):
        info = owned(record.stem)
        if info:
            pids.add(info["pid"])
    backend = log_dir() / "backend.pid"
    if backend.is_file() and backend.read_text().strip().isdigit():
        pids.add(int(backend.read_text().strip()))
    return pids


def require_free_gpu() -> None:
    foreign = [(pid, mib) for pid, mib in gpu_users() if pid not in our_pids()]
    if foreign:
        listing = ", ".join(f"pid {p} ({m} MiB)" for p, m in foreign)
        raise SystemExit(f"[hapilotctl] the GPU is in use by other processes: {listing}. Not starting models — "
                         "wait until that job finishes.")


# --------------------------------------------------------------------------- commands

def cmd_setup(args) -> None:
    from src.privacy import apply_offline_env

    for sub in ("run", "log", "workspace", "task_states", "chroma"):
        (var_dir() / sub).mkdir(parents=True, exist_ok=True)
    if not MODELS_LOCAL.is_file():
        shutil.copy(ROOT / "config" / "models.example.yaml", MODELS_LOCAL)
        say(f"created {MODELS_LOCAL.relative_to(ROOT)} — check the model paths")
    secret = var_dir() / "chainlit_secret"
    if not secret.is_file():
        secret.write_text(secrets.token_urlsafe(48))
        secret.chmod(0o600)
    if not args.skip_image:
        image = get_settings().sandbox_image
        say(f"building sandbox image {image} (downloads the base image and TeX Live once)")
        subprocess.run(["docker", "build", "-t", image, str(ROOT / "sandbox")], check=True)
    if not args.skip_embeddings:
        apply_offline_env(allow_downloads=True)
        from huggingface_hub import snapshot_download

        model = get_settings().embedding_model
        say(f"downloading embedding model {model} (once; afterwards Hapilot runs offline)")
        snapshot_download(model, allow_patterns=EMBEDDING_FILES)
    say("setup done")


def cmd_ingest(args) -> None:
    cmd = [str(PY), "-m", "src.ingest_data"] + (["--docs", str(args.docs)] if args.docs else []) + (["--force"] if args.force else [])
    subprocess.run(cmd, cwd=ROOT, check=True)


def models_up(profile: str | None, preload: bool = True, wrap: list[str] | None = None) -> None:
    if not MODELS_LOCAL.is_file():
        raise SystemExit("[hapilotctl] config/models.local.yaml missing — run setup first")
    if not owned("modelswap"):
        require_free_gpu()
    cmd = (wrap or []) + [str(PY), str(ROOT / "tools" / "modelswap.py"), "--config", str(MODELS_LOCAL),
                          "--port", str(PROXY_PORT), "--backend-port", str(BACKEND_PORT), "--log-dir", str(log_dir())]
    start("modelswap", cmd, marker="modelswap.py", port=PROXY_PORT, wait_path="/health")
    if preload:
        model = get_settings().profile(profile).default_model().model
        say(f"loading {model} …")
        req = urllib.request.Request(f"http://127.0.0.1:{PROXY_PORT}/admin/load", data=json.dumps({"model": model}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=900) as resp:
            say(f"model ready: {json.loads(resp.read()).get('model')}")


def models_down() -> None:
    stop("modelswap")
    stop_backend_leftover()


def ui_env() -> dict:
    env = {"HAPILOT_MODEL_URL": f"http://127.0.0.1:{PROXY_PORT}/v1"}
    users = os.environ.get("HAPILOT_UI_USERS_FILE")
    if users and Path(users).expanduser().is_file():
        env["CHAINLIT_AUTH_SECRET"] = (var_dir() / "chainlit_secret").read_text().strip()
    return env


def ui_up(profile: str | None, wrap: list[str] | None = None) -> None:
    chainlit = PY.parent / "chainlit"
    env = ui_env()
    if profile:
        env["HAPILOT_PROFILE"] = profile
    start("ui", (wrap or []) + [str(chainlit), "run", "src/app.py", "--host", "127.0.0.1", "--port", str(UI_PORT), "--headless"],
          marker="src/app.py", port=UI_PORT, env=env)


def cmd_models(args) -> None:
    if args.action == "up":
        models_up(args.profile, preload=not args.no_preload)
    elif args.action == "down":
        models_down()
    else:
        cmd_status(args)


def cmd_ui(args) -> None:
    ui_up(args.profile) if args.action == "up" else stop("ui")


def cmd_up(args) -> None:
    models_up(args.profile)
    cmd_ingest(argparse.Namespace(docs=None, force=False))
    ui_up(args.profile)
    say(f"open the UI through an SSH tunnel: {tunnel_command()}")


def cmd_down(args) -> None:
    stop("ui")
    models_down()


def cmd_status(args) -> None:
    for name in ("modelswap", "ui"):
        info = owned(name)
        say(f"{name:9s}: " + (f"running, pid {info['pid']}, 127.0.0.1:{info['port']}, since {info['started']}" if info else "stopped"))
    if owned("modelswap"):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PROXY_PORT}/health", timeout=3) as resp:
                say(f"loaded model: {json.loads(resp.read()).get('loaded')}")
        except Exception as exc:
            say(f"proxy not answering: {exc}")
    ours = our_pids()
    for pid, mib in gpu_users():
        say(f"GPU: pid {pid} uses {mib} MiB {'(Hapilot)' if pid in ours else '(other process)'}")


def tunnel_command() -> str:
    return f"ssh -N -L {UI_PORT}:127.0.0.1:{UI_PORT} <user>@<gpu-host>   then open http://localhost:{UI_PORT}"


def cmd_tunnel(args) -> None:
    print(tunnel_command())


def cmd_add_user(args) -> None:
    from src.ui_auth import hash_password

    path = Path(os.environ.get("HAPILOT_UI_USERS_FILE", str(var_dir() / "ui_users.txt"))).expanduser()
    password = getpass.getpass(f"Password for {args.name}: ")
    if len(password) < 10 or password != getpass.getpass("Repeat: "):
        raise SystemExit("[hapilotctl] passwords differ or shorter than 10 characters")
    lines = [ln for ln in (path.read_text().splitlines() if path.is_file() else []) if not ln.startswith(f"{args.name}:")]
    lines.append(f"{args.name}:{hash_password(password)}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    say(f"user {args.name} saved in {path}; set HAPILOT_UI_USERS_FILE={path} in .env to enable login")


def cmd_eval(args) -> None:
    """Evaluation run (docs/EVALUATION_INTERFACE.md): model proxy up if needed, run, and stop what was started."""
    started_here = not owned("modelswap")
    if started_here:
        models_up(args.profile)
    cmd = [str(PY), "-m", "src.eval", "run", "--tasks", str(args.tasks), "--corpus", str(args.corpus), "--today", args.today,
           "--out", str(args.out)]
    for flag, value in (("--profile", args.profile), ("--answer-mode", args.answer_mode), ("--timeout", args.timeout),
                        ("--only", args.only)):
        if value is not None:
            cmd += [flag, str(value)]
    if args.retry_failed:
        cmd.append("--retry-failed")
    code = subprocess.run(cmd, cwd=ROOT, env={**os.environ, "HAPILOT_MODEL_URL": f"http://127.0.0.1:{PROXY_PORT}/v1"}).returncode
    if started_here:
        models_down()
    raise SystemExit(code)


def cmd_smoke(args) -> None:
    if not owned("modelswap"):
        models_up(args.profile)
    cmd_ingest(argparse.Namespace(docs=None, force=False))
    cmd = [str(PY), str(ROOT / "scripts" / "smoke.py"), "--model-url", f"http://127.0.0.1:{PROXY_PORT}/v1"]
    if args.profile:
        cmd += ["--profile", args.profile]
    raise SystemExit(subprocess.run(cmd, cwd=ROOT).returncode)


# --------------------------------------------------------------------------- egress test

_CONNECT = re.compile(r"connect\(\d+, \{sa_family=(AF_INET6?),.*?(?:htons\((\d+)\)).*?"
                      r"(?:inet_addr\(\"([^\"]+)\"\)|inet_pton\(AF_INET6, \"([^\"]+)\")")


def strace_violations(log: Path) -> list[str]:
    """connect() calls to non-local addresses, or to DNS (port 53), in an strace log."""
    import ipaddress

    bad = []
    for line in log.read_text(errors="replace").splitlines():
        m = _CONNECT.search(line)
        if not m:
            continue
        port = int(m.group(2))
        addr = m.group(3) or m.group(4)
        ip = ipaddress.ip_address(addr.split("%")[0])
        if port == 53 or not (ip.is_loopback or ip.is_unspecified or (ip.is_private and port != 53)):
            bad.append(line.strip()[:200])
    return bad


def git_state() -> dict:
    def run(*a):
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()

    return {"commit": run("rev-parse", "HEAD"), "describe": run("describe", "--always", "--dirty", "--tags"),
            "dirty": bool(run("status", "--porcelain", "--untracked-files=no"))}


def cmd_egress_test(args) -> None:
    strace = shutil.which("strace")
    if not strace:
        raise SystemExit("[hapilotctl] strace is required for the egress test")
    out = var_dir() / "egress" / dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    base = [strace, "-f", "-qq", "-e", "trace=connect"]
    if "--seccomp-bpf" in subprocess.run([strace, "-h"], capture_output=True, text=True).stdout:
        base.append("--seccomp-bpf")

    cmd_down(args)
    say("egress test: model proxy, UI and scenario suite run under strace; Python sockets are guarded")
    models_up(args.profile, wrap=base + ["-o", str(out / "strace-models.log"), "--"])
    ui_up(args.profile, wrap=base + ["-o", str(out / "strace-ui.log"), "--"])
    urllib.request.urlopen(f"http://127.0.0.1:{UI_PORT}/", timeout=30).read()
    subprocess.run(base + ["-o", str(out / "strace-ingest.log"), "--", str(PY), "-m", "src.ingest_data", "--force"],
                   cwd=ROOT, check=True)
    smoke = subprocess.run(base + ["-o", str(out / "strace-smoke.log"), "--", str(PY), str(ROOT / "scripts" / "smoke.py"),
                                   "--model-url", f"http://127.0.0.1:{PROXY_PORT}/v1", "--egress-guard",
                                   "--report", str(out / "smoke.json")] + (["--profile", args.profile] if args.profile else []),
                           cwd=ROOT)
    cmd_down(args)

    violations = {name: strace_violations(out / f"strace-{name}.log") for name in ("models", "ui", "ingest", "smoke")}
    smoke_report = json.loads((out / "smoke.json").read_text()) if (out / "smoke.json").is_file() else {}
    guard = smoke_report.get("egress_guard_attempts", [])
    sandbox_modes = sorted({r.get("network_mode") for r in smoke_report.get("sandbox_runs", [])})
    passed = (not any(violations.values()) and not guard and smoke.returncode == 0 and sandbox_modes in ([], ["none"]))
    report = {"date": dt.datetime.now().isoformat(timespec="seconds"), **git_state(),
              "profile": args.profile or get_settings().default_profile, "passed": passed,
              "strace_violations": violations, "egress_guard_attempts": guard, "sandbox_network_modes": sandbox_modes,
              "scenarios": smoke_report.get("summary"), "logs": str(out)}
    (out / "egress_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    say(json.dumps({k: report[k] for k in ("date", "describe", "profile", "passed", "scenarios", "sandbox_network_modes")},
                   ensure_ascii=False))
    for name, lines in violations.items():
        for line in lines[:10]:
            say(f"VIOLATION ({name}): {line}")
    raise SystemExit(0 if passed else 1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Hapilot launcher (GPU host)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("setup")
    p.add_argument("--skip-image", action="store_true")
    p.add_argument("--skip-embeddings", action="store_true")
    p.set_defaults(func=cmd_setup)
    p = sub.add_parser("ingest")
    p.add_argument("--docs", type=Path)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_ingest)
    p = sub.add_parser("models")
    p.add_argument("action", choices=["up", "down", "status"])
    p.add_argument("--profile")
    p.add_argument("--no-preload", action="store_true")
    p.set_defaults(func=cmd_models)
    p = sub.add_parser("ui")
    p.add_argument("action", choices=["up", "down"])
    p.add_argument("--profile")
    p.set_defaults(func=cmd_ui)
    for name, func in (("up", cmd_up), ("smoke", cmd_smoke), ("egress-test", cmd_egress_test)):
        p = sub.add_parser(name)
        p.add_argument("--profile")
        p.set_defaults(func=func)
    for name, func in (("down", cmd_down), ("status", cmd_status), ("tunnel", cmd_tunnel)):
        sub.add_parser(name).set_defaults(func=func, profile=None)
    p = sub.add_parser("eval")
    p.add_argument("--tasks", type=Path, required=True)
    p.add_argument("--corpus", type=Path, required=True)
    p.add_argument("--today", required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--profile")
    p.add_argument("--answer-mode", choices=["formatter", "parse"])
    p.add_argument("--timeout", type=float)
    p.add_argument("--only")
    p.add_argument("--retry-failed", action="store_true")
    p.set_defaults(func=cmd_eval)
    p = sub.add_parser("add-user")
    p.add_argument("name")
    p.set_defaults(func=cmd_add_user)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
