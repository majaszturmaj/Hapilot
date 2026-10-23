#!/usr/bin/env python3
"""Hapilot launcher for the GPU host — starts and stops only what it started itself.

    .venv/bin/python scripts/hapilotctl.py setup            # sandbox image, embedding model, config files
    .venv/bin/python scripts/hapilotctl.py up [--profile P] # model proxy + knowledge base + UI
    .venv/bin/python scripts/hapilotctl.py status | down
    .venv/bin/python scripts/hapilotctl.py smoke [--profile P]
    .venv/bin/python scripts/hapilotctl.py egress-test [--profile P]
    .venv/bin/python scripts/hapilotctl.py integration [--profile P ...]   # smoke + planner + dev tasks, report
    .venv/bin/python scripts/hapilotctl.py add-user NAME | tunnel
    .venv/bin/python scripts/hapilotctl.py make-cert --ip ADDRESS     # self-signed HTTPS certificate for the UI
    .venv/bin/python scripts/hapilotctl.py update [--keep-running]    # follow a git branch: test, switch, roll back

Safety rules built in:
- every process it starts is recorded in var/run/<name>.json and stopped by its own process group,
  after checking that the PID still runs the same command (no pkill, no killing by pattern);
- ports are checked before starting; everything binds to 127.0.0.1, except the UI when the operator sets
  HAPILOT_UI_HOST (local network access, docs/deployment.md); model servers always stay on 127.0.0.1;
- the model proxy is not started while another process uses the GPU;
- Docker is used only to build the sandbox image (setup) and, from Hapilot itself, for sandbox runs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import get_settings  # noqa: E402

PY = Path(sys.executable)
# Ports come from the environment or .env (HAPILOT_UI_PORT, HAPILOT_MODELSWAP_PORT, HAPILOT_BACKEND_PORT).
UI_PORT = get_settings().ui_port
PROXY_PORT = get_settings().modelswap_port
BACKEND_PORT = get_settings().backend_port
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


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    """True unless something listens on host:port (connections in TIME_WAIT do not count, as for the servers)."""
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            s.listen(1)
            return True
        except OSError:
            return False


def local_url(host: str, port: int, tls: bool = False, path: str = "") -> str:
    """URL of a server bound to host:port, as seen from this machine (0.0.0.0 → 127.0.0.1)."""
    address = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    return f"{'https' if tls else 'http'}://{f'[{address}]' if ':' in address else address}:{port}{path}"


def fetch(url: str, timeout: float = 10) -> bytes:
    """GET from one of our own servers; the UI's own self-signed certificate is accepted (liveness checks only)."""
    context = ssl._create_unverified_context() if url.startswith("https:") else None
    with urllib.request.urlopen(url, timeout=timeout, context=context) as resp:
        return resp.read()


def start(name: str, cmd: list[str], marker: str, port: int, env: dict | None = None, wait_path: str = "/",
          host: str = "127.0.0.1", tls: bool = False) -> dict:
    if owned(name):
        say(f"{name} already running")
        return owned(name)  # type: ignore[return-value]
    if not port_free(port, host):
        raise SystemExit(f"[hapilotctl] port {host}:{port} is in use by another process — "
                         f"choose another port (see README, 'Ports').")
    log = open(log_dir() / f"{name}.log", "ab")
    proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                            env={**os.environ, **(env or {})})
    info = {"name": name, "pid": proc.pid, "pgid": os.getpgid(proc.pid), "marker": marker, "port": port, "host": host,
            "started": dt.datetime.now().isoformat(timespec="seconds"), "cmd": cmd}
    (run_dir() / f"{name}.json").write_text(json.dumps(info, indent=2))
    for _ in range(180):
        if proc.poll() is not None:
            raise SystemExit(f"[hapilotctl] {name} exited (code {proc.returncode}); see {log.name}")
        try:
            fetch(local_url(host, port, tls, wait_path), timeout=2)
            say(f"{name} up on {host}:{port} (pid {proc.pid})")
            return info
        except Exception:
            time.sleep(1)
    say(f"{name} started (pid {proc.pid}) but {host}:{port} does not answer yet; see {log.name}")
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


def ui_stopped_marker() -> Path:
    """Present while an operator has stopped the UI (``ui down`` / ``down``): ``ui ensure`` then leaves it off."""
    return run_dir() / "ui.stopped"


def ui_command() -> list[str]:
    s = get_settings()
    cmd = [str(PY.parent / "chainlit"), "run", "src/app.py", "--host", s.ui_host, "--port", str(UI_PORT), "--headless"]
    if s.ui_ssl_cert and s.ui_ssl_key:
        missing = [str(p) for p in (s.ui_ssl_cert, s.ui_ssl_key) if not p.is_file()]
        if missing:
            raise SystemExit(f"[hapilotctl] HTTPS certificate or key not found: {', '.join(missing)} "
                             "(scripts/hapilotctl.py make-cert creates them)")
        cmd += ["--ssl-cert", str(s.ui_ssl_cert), "--ssl-key", str(s.ui_ssl_key)]
    return cmd


def ui_up(profile: str | None, wrap: list[str] | None = None) -> None:
    env = ui_env()
    if profile:
        env["HAPILOT_PROFILE"] = profile
    s = get_settings()
    start("ui", (wrap or []) + ui_command(), marker="src/app.py", port=UI_PORT, env=env, host=s.ui_host,
          tls=bool(s.ui_ssl_cert))
    ui_stopped_marker().unlink(missing_ok=True)


def ui_down() -> None:
    stop("ui")
    ui_stopped_marker().write_text(dt.datetime.now().isoformat(timespec="seconds"))


def ui_ensure(profile: str | None = None) -> None:
    """Start the UI unless it runs or an operator stopped it; for cron (@reboot and the updater)."""
    if not owned("ui") and not ui_stopped_marker().is_file():
        ui_up(profile)


def ui_address() -> str:
    """Where people open the UI."""
    s = get_settings()
    if ipaddress.ip_address(s.ui_host).is_loopback:
        return f"through an SSH tunnel: {tunnel_command()}"
    host = s.ui_host if not ipaddress.ip_address(s.ui_host).is_unspecified else "<this machine's address>"
    return f"from the local network: {'https' if s.ui_ssl_cert else 'http'}://{host}:{UI_PORT}"


def cmd_models(args) -> None:
    if args.action == "up":
        models_up(args.profile, preload=not args.no_preload)
    elif args.action == "down":
        models_down()
    else:
        cmd_status(args)


def cmd_ui(args) -> None:
    if args.action == "up":
        ui_up(args.profile)
        say(f"open the UI {ui_address()}")
    elif args.action == "down":
        ui_down()
    elif args.action == "restart":
        stop("ui")
        ui_up(args.profile)
    else:
        ui_ensure(args.profile)


def cmd_up(args) -> None:
    models_up(args.profile)
    cmd_ingest(argparse.Namespace(docs=None, force=False))
    ui_up(args.profile)
    say(f"open the UI {ui_address()}")


def cmd_down(args) -> None:
    ui_down()
    models_down()


def cmd_status(args) -> None:
    for name in ("modelswap", "ui"):
        info = owned(name)
        say(f"{name:9s}: " + (f"running, pid {info['pid']}, {info.get('host', '127.0.0.1')}:{info['port']}, "
                              f"since {info['started']}" if info else "stopped"))
    if owned("modelswap"):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PROXY_PORT}/health", timeout=3) as resp:
                say(f"loaded model: {json.loads(resp.read()).get('loaded')}")
        except Exception as exc:
            say(f"proxy not answering: {exc}")
    ours = our_pids()
    for pid, mib in gpu_users():
        say(f"GPU: pid {pid} uses {mib} MiB {'(Hapilot)' if pid in ours else '(other process)'}")
    update = read_update_status()
    if update:
        say(f"update   : {update['state']} (checked {update['time']}), commit {update['commit'][:8]} — {update['message']}")


def tunnel_command() -> str:
    s = get_settings()
    target = local_url(s.ui_host, UI_PORT).split("://", 1)[1].rsplit(":", 1)[0]
    return (f"ssh -N -L {UI_PORT}:{target}:{UI_PORT} <user>@<gpu-host>   then open "
            f"{'https' if s.ui_ssl_cert else 'http'}://localhost:{UI_PORT}")


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
    started_here = not owned("modelswap")
    if started_here:
        models_up(args.profile)
    cmd = [str(PY), str(ROOT / "scripts" / "smoke.py"), "--model-url", f"http://127.0.0.1:{PROXY_PORT}/v1"]
    if args.profile:
        cmd += ["--profile", args.profile]
    code = subprocess.run(cmd, cwd=ROOT).returncode
    if started_here:
        models_down()
    raise SystemExit(code)


def cmd_integration(args) -> None:
    """Integration suite (scripts/integration.py); extra arguments are passed through."""
    raise SystemExit(subprocess.run([str(PY), str(ROOT / "scripts" / "integration.py"), *args.rest], cwd=ROOT).returncode)


# --------------------------------------------------------------------------- HTTPS for the UI

def cmd_make_cert(args) -> None:
    """Self-signed certificate for the UI on a local network (browsers warn once; compare the fingerprint)."""
    out = (args.out or var_dir() / "tls").expanduser()
    cert, key = out / "hapilot.crt", out / "hapilot.key"
    if (cert.exists() or key.exists()) and not args.force:
        raise SystemExit(f"[hapilotctl] {cert} or {key} exists; --force replaces them")
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    names = ["DNS:localhost", "IP:127.0.0.1"] + [f"IP:{ipaddress.ip_address(ip)}" for ip in args.ip] + \
        [f"DNS:{name}" for name in args.dns]
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:3072", "-sha256", "-days", str(args.days), "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-subj", "/CN=Hapilot",
                    "-addext", "subjectAltName=" + ",".join(names)], check=True, capture_output=True)
    key.chmod(0o600)
    fingerprint = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-fingerprint", "-sha256"],
                                 check=True, capture_output=True, text=True).stdout.strip().split("=", 1)[-1]
    say(f"certificate {cert} ({', '.join(names)}; {args.days} days), key {key}")
    say(f"set in .env: HAPILOT_UI_SSL_CERT={cert} and HAPILOT_UI_SSL_KEY={key}")
    say(f"SHA-256 fingerprint (the browser's certificate details must show the same): {fingerprint}")


# --------------------------------------------------------------------------- updates (installs that follow a branch)

REQUIREMENTS = ("requirements.txt", "requirements-test.txt")


class UpdateError(RuntimeError):
    """An update step failed; the install stays on (or goes back to) the previous commit."""


def git(*args: str, cwd: Path | None = None) -> str:
    out = subprocess.run(["git", *args], cwd=cwd or ROOT, capture_output=True, text=True)
    if out.returncode:
        raise UpdateError(f"git {' '.join(args)}: {(out.stderr or out.stdout).strip()[-300:]}")
    return out.stdout.strip()


def update_status_path() -> Path:
    return run_dir() / "update.json"


def read_update_status() -> dict:
    try:
        return json.loads(update_status_path().read_text())
    except (OSError, ValueError):
        return {}


def record_update(state: str, message: str, log: bool = True, **fields) -> dict:
    """Status of the last check (var/run/update.json); changes are also appended to var/log/update.log."""
    previous = read_update_status()
    status = {"state": state, "time": dt.datetime.now().isoformat(timespec="seconds"), "commit": git("rev-parse", "HEAD"),
              "message": message, "failed_target": previous.get("failed_target"),
              "last_update": previous.get("last_update"), **fields}
    tmp = update_status_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=2))
    os.replace(tmp, update_status_path())
    if log:
        with open(log_dir() / "update.log", "a", encoding="utf-8") as fh:
            fh.write(f"{status['time']} {state} {message}\n")
        say(f"update: {state} — {message}")
    return status


def backup_data(commit: str, keep: int = 10) -> Path:
    """Copies of what people created — chats, accounts, custom nodes, templates, prompt edits, signatures — before
    an update. Workspaces and the knowledge base are not copied: updates do not change them (ingest rebuilds the
    index)."""
    import sqlite3
    from contextlib import closing

    from src import admin, doc_templates
    from src.ui_auth import users_file

    root = var_dir() / "backups"
    dest = root / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{commit[:8]}"
    dest.mkdir(parents=True)
    dest.chmod(0o700)
    chat_db = Path(os.environ.get("HAPILOT_CHAT_DB", "").strip() or var_dir() / "chat_history.sqlite").expanduser()
    if chat_db.is_file():  # same default as src/ui_history.db_path; the online backup is consistent while the UI runs
        with closing(sqlite3.connect(chat_db)) as source, closing(sqlite3.connect(dest / "chat_history.sqlite")) as copy:
            source.backup(copy)
    items = {"ui_users": users_file(), "custom_extensions": admin.custom_dir(), "prompt_overrides": admin.overrides_dir(),
             "admin_audit.jsonl": admin.audit_path(), "custom_templates": doc_templates.custom_dir(),
             "template_overrides": doc_templates.overrides_dir(), "signatures": var_dir() / "signatures"}
    for name, path in items.items():
        if path and path.is_dir():
            shutil.copytree(path, dest / name)
        elif path and path.is_file():
            shutil.copy2(path, dest / name)
    for old in sorted(p for p in root.iterdir() if p.is_dir())[:-keep]:
        shutil.rmtree(old)
    return dest


def install_requirements(cwd: Path) -> None:
    proc = subprocess.run([str(PY), "-m", "pip", "install", "-q", "-r", "requirements.txt", "-r", "requirements-test.txt"],
                          cwd=cwd, capture_output=True, text=True, timeout=1800)
    if proc.returncode:
        raise UpdateError(f"pip install failed: {(proc.stderr or proc.stdout).strip()[-300:]}")


def prepare_commit(target: str, requirements: bool, sandbox: bool) -> str:
    """Check out ``target`` next to the running install and get it ready: dependencies, sandbox image, unit tests.
    The running UI keeps its files until the switch. Returns pytest's summary line."""
    work = var_dir() / "update" / "worktree"
    if work.exists():
        shutil.rmtree(work)
    git("worktree", "prune")
    work.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "--detach", "--force", str(work), target)
    # the install's settings (.env, loaded into this process) must not reach the tests or the image name
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HAPILOT_", "CHAINLIT_"))}
    try:
        if requirements:
            install_requirements(work)
        if sandbox:
            image = subprocess.run([str(PY), "-c", "from src.config import get_settings; print(get_settings().sandbox_image)"],
                                   cwd=work, env=env, capture_output=True, text=True, check=True).stdout.strip()
            subprocess.run(["docker", "build", "-q", "-t", image, str(work / "sandbox")], check=True, capture_output=True,
                           timeout=3600)
        try:
            proc = subprocess.run([str(PY), "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"], cwd=work, env=env,
                                  capture_output=True, text=True, timeout=1800)
        except subprocess.TimeoutExpired:
            raise UpdateError("unit tests did not finish within 30 min") from None
    finally:
        shutil.rmtree(work, ignore_errors=True)
        git("worktree", "prune")
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    summary = lines[-1].strip("= ") if lines else (proc.stderr.strip()[-200:] or f"exit code {proc.returncode}")
    if proc.returncode:
        (log_dir() / "update-tests.log").write_text(proc.stdout[-50000:] + "\n" + proc.stderr[-10000:])
        raise UpdateError(f"unit tests of {target[:8]} failed: {summary} (log/update-tests.log)")
    return summary


def restart_ui() -> None:
    """Restart the UI with the launcher of the commit now checked out (it knows how its own UI starts)."""
    proc = subprocess.run([str(PY), str(ROOT / "scripts" / "hapilotctl.py"), "ui", "restart"], cwd=ROOT,
                          capture_output=True, text=True, timeout=600)
    if proc.returncode:
        raise UpdateError(f"UI restart failed: {(proc.stderr or proc.stdout).strip()[-300:]}")


def ui_health(timeout: float = 120) -> None:
    """The UI answers, and asks for a login when accounts are configured."""
    s = get_settings()
    deadline, last = time.time() + timeout, ""
    while time.time() < deadline:
        try:
            fetch(local_url(s.ui_host, UI_PORT, bool(s.ui_ssl_cert), "/"))
            config = json.loads(fetch(local_url(s.ui_host, UI_PORT, bool(s.ui_ssl_cert), "/auth/config")))
        except Exception as exc:
            last = str(exc)
            time.sleep(2)
            continue
        if os.environ.get("HAPILOT_UI_USERS_FILE", "").strip() and not config.get("requireLogin"):
            raise UpdateError("the UI answers without asking for a login although HAPILOT_UI_USERS_FILE is set")
        return
    raise UpdateError(f"the UI does not answer: {last}")


def update(branch: str, now: bool = False, idle_minutes: float = 10, retry: bool = False) -> int:
    """Follow ``origin/<branch>``: back up, prepare and test the new commit, switch, restart the UI if it runs,
    check it; on any failure go back to the previous commit. Waits while people use the UI (unless ``now``)."""
    from src import ui_activity

    if git("status", "--porcelain", "--untracked-files=no"):
        record_update("error", "tracked files were changed in this checkout; not updating (git status)")
        return 1
    git("fetch", "--quiet", "origin", branch)
    old, target = git("rev-parse", "HEAD"), git("rev-parse", f"origin/{branch}")
    if target == old:
        record_update("current", f"{branch} = {old[:8]}", log=False)
        return 0
    if target == read_update_status().get("failed_target") and not retry:
        record_update("skipped", f"{target[:8]} failed before; waiting for a newer commit on {branch} "
                      "(update --retry tries it again)", log=False)
        return 0
    ui = owned("ui")
    if ui and not now:
        free, why = ui_activity.idle(ui["pid"], idle_minutes * 60)
        if not free:
            record_update("deferred", f"{target[:8]} waits: {why}", log=False)
            return 0
    say(f"updating {old[:8]} → {target[:8]} (origin/{branch})")
    backup = backup_data(old)
    changed = git("diff", "--name-only", old, target).splitlines()
    requirements = any(name in REQUIREMENTS for name in changed)
    switched = False
    try:
        summary = prepare_commit(target, requirements, sandbox=any(name.startswith("sandbox/") for name in changed))
        git("reset", "--hard", "--quiet", target)
        switched = True
        if ui:
            restart_ui()
            ui_health()
    except (UpdateError, subprocess.SubprocessError, OSError) as exc:
        try:
            if switched:
                git("reset", "--hard", "--quiet", old)
            if requirements:
                install_requirements(ROOT)
            if switched and ui:
                restart_ui()
                ui_health()
        except (UpdateError, subprocess.SubprocessError, OSError) as again:
            record_update("error", f"update to {target[:8]} failed ({exc}); going back to {old[:8]} failed too "
                          f"({again}) — needs a look by hand", failed_target=target, backup=str(backup))
            return 1
        record_update("rolled_back", f"update to {target[:8]} failed: {exc}; still on {old[:8]}",
                      failed_target=target, backup=str(backup))
        return 1
    record_update("updated", f"{old[:8]} → {target[:8]}; unit tests: {summary}", failed_target=None,
                  last_update={"from": old, "to": target, "time": dt.datetime.now().isoformat(timespec="seconds"),
                               "backup": str(backup)})
    return 0


def cmd_update(args) -> None:
    import fcntl

    lock = open(run_dir() / "update.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        say("another update is running")
        return
    try:
        code = update(args.branch or get_settings().update_branch, now=args.now, idle_minutes=args.idle_minutes,
                      retry=args.retry)
    except UpdateError as exc:  # e.g. GitHub not reachable: nothing changed
        record_update("error", str(exc))
        code = 1
    if args.keep_running:  # with the launcher of whatever commit is checked out now
        subprocess.run([str(PY), str(ROOT / "scripts" / "hapilotctl.py"), "ui", "ensure"], cwd=ROOT)
    raise SystemExit(code)


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
    p.add_argument("action", choices=["up", "down", "restart", "ensure"])
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
    p = sub.add_parser("integration", add_help=False, help="integration suite on the GPU host (options: integration --help)")
    p.set_defaults(func=cmd_integration)
    p = sub.add_parser("add-user")
    p.add_argument("name")
    p.set_defaults(func=cmd_add_user)
    p = sub.add_parser("make-cert", help="self-signed HTTPS certificate for the UI")
    p.add_argument("--ip", action="append", default=[], help="address people use to open the UI (repeatable)")
    p.add_argument("--dns", action="append", default=[], help="host name people use to open the UI (repeatable)")
    p.add_argument("--days", type=int, default=825)
    p.add_argument("--out", type=Path, help="folder (default: <data dir>/tls)")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_make_cert)
    p = sub.add_parser("update", help="follow a git branch: back up, test, switch, restart, roll back on failure")
    p.add_argument("--branch", help="default: HAPILOT_UPDATE_BRANCH or main")
    p.add_argument("--now", action="store_true", help="do not wait until nobody uses the UI")
    p.add_argument("--idle-minutes", type=float, default=10, help="minutes without UI activity before a restart")
    p.add_argument("--retry", action="store_true", help="try a commit that failed before again")
    p.add_argument("--keep-running", action="store_true", help="afterwards start the UI if it is not running "
                   "(unless stopped with 'ui down')")
    p.set_defaults(func=cmd_update)
    args, rest = parser.parse_known_args()
    if args.command == "integration":
        args.rest = rest  # options belong to scripts/integration.py
    elif rest:
        parser.error(f"unrecognized arguments: {' '.join(rest)}")
    args.func(args)


if __name__ == "__main__":
    main()
