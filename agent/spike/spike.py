#!/usr/bin/env python3
"""Hapilot v3, stage 0: run a task set through the agent container, one task per fresh container, and score it.

The set-up follows earlier benchmark runs of the Cline CLI so the results can be compared: the documents as
originals plus one text copy per document (same path + .txt), the task prompt from a template file kept outside the
repository (system prompt, task block, workspace note), the answer as one JSON object in `odpowiedz.json` in the working
directory, 900 s per task, an internal Docker network whose only reachable host is the model relay `model:8000`.
Unlike those runs the documents are mounted read-only, and the container runs with a read-only root filesystem, no
capabilities and a process limit.

  prepare  text copies of a corpus with the task set's own extractor (<set>/harness/corpus.py)
  run      run tasks (resumable): results.jsonl, per task logi/<id>/{spec.json,result.json,trace.jsonl,egress.jsonl}
  score    deterministic scoring with the task set's harness/scoring.py: ocena.jsonl and summary.json

The task set is a directory with harness/{scoring.py,corpus.py}, korpus/{dokumenty/,manifest.json} and YAML tasks
(id, polecenie, format_odpowiedzi, sprawdzenia, ...). Nothing from it is copied into this repository.
"""

import argparse
import hashlib
import ipaddress
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

WEEKDAYS = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
MONTHS = ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia", "września",
          "października", "listopada", "grudnia"]
RUNNER = "/opt/hapilot-agent"
GRACE_S = 60


# --------------------------------------------------------------------------- prompt

def polish_date(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]} {d.year} r."


def build_prompt(template: dict, task: dict, firma: str, dzis: date) -> str:
    """Template keys: system (placeholders {firma}, {data}), task ({polecenie}, {format}), workspace_note."""
    fmt = json.dumps(task["format_odpowiedzi"], ensure_ascii=False, indent=2)
    system = template["system"].format(firma=firma, data=polish_date(dzis))
    user = template["task"].format(polecenie=task["polecenie"].strip(), format=fmt)
    return system + "\n\n" + user + template.get("workspace_note", "")


# --------------------------------------------------------------------------- egress (outside the Node guard)

def public_connections(proc_net_tcp: str) -> set[str]:
    """Remote addresses of ESTABLISHED/SYN_SENT TCP entries (/proc/net/tcp, tcp6) that are not private or loopback."""
    seen = set()
    for line in proc_net_tcp.splitlines():
        parts = line.split()
        if len(parts) < 4 or ":" not in parts[2] or parts[3] not in ("01", "02"):
            continue
        addr_hex, port_hex = parts[2].split(":")
        try:
            raw = bytes.fromhex(addr_hex)
            if len(raw) == 4:
                ip = ipaddress.IPv4Address(raw[::-1])
            elif len(raw) == 16:
                v6 = ipaddress.IPv6Address(b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4)))
                ip = v6.ipv4_mapped or v6
            else:
                continue
        except ValueError:
            continue
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            seen.add(f"{ip}:{int(port_hex, 16)}")
    return seen


def watch_container(name: str, out: Path, stop: threading.Event, every: float = 2.0) -> None:
    while not stop.is_set():
        r = subprocess.run(["docker", "exec", name, "cat", "/proc/net/tcp", "/proc/net/tcp6"],
                           capture_output=True, text=True)
        for remote in sorted(public_connections(r.stdout)):
            with open(out, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "container": name,
                                     "remote": remote}) + "\n")
        stop.wait(every)


# --------------------------------------------------------------------------- tasks and answers

def load_tasks(d: Path) -> list[dict]:
    import yaml
    return sorted((yaml.safe_load(f.read_text(encoding="utf-8")) for f in d.rglob("*.yaml")), key=lambda t: t["id"])


def load_harness(task_set: Path):
    sys.path.insert(0, str(task_set / "harness"))
    import scoring  # noqa: PLC0415  (the task set's own scorer)
    return scoring


def load_manifest(korpus: Path) -> dict:
    """{document id: manifest entry}, as the task set's corpus.load_manifest builds it (without its extractor imports)."""
    data = json.loads((korpus / "manifest.json").read_text(encoding="utf-8"))
    return {d["id"]: d for d in data["dokumenty"]}


def summarize_trace(trace_path: Path) -> dict:
    tools, errors, iterations, notices = {}, 0, 0, []
    if trace_path.is_file():
        for line in trace_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("type") == "tool_end":
                tools[e.get("tool")] = tools.get(e.get("tool"), 0) + 1
                errors += bool(e.get("error"))
            elif e.get("type") == "iteration":
                iterations = max(iterations, int(e.get("n") or 0))
            elif e.get("type") == "notice":
                notices.append(e.get("reason") or e.get("notice"))
    return {"tool_calls": tools, "tool_errors": errors, "iterations": iterations, "notices": notices}


def make_row(task_id: str, answer: str, parse_answer, result: dict, status: str, error, duration: float,
             exit_code, trace: dict, egress_node: int, egress_proc: int) -> dict:
    parsed = parse_answer(answer) if answer else None
    if status == "ok" and not answer:
        error = error or f"no odpowiedz.json (exit {exit_code})"
    return {"task_id": task_id, "status": status, "error": error, "answer_text": answer, "answer_json": parsed,
            "answer_json_valid": parsed is not None, "duration_s": duration, "sources_used": [],
            "exit_code": exit_code, "finish_reason": result.get("finish_reason"), "submitted": result.get("submitted"),
            "usage": result.get("usage"), "trace": trace, "egress_blocked_node": egress_node,
            "egress_public_tcp": egress_proc}


# --------------------------------------------------------------------------- commands

def prepare(a) -> None:
    task_set = Path(a.set).expanduser()
    sys.path.insert(0, str(task_set / "harness"))
    from corpus import load_corpus  # noqa: PLC0415
    korpus = Path(a.korpus).expanduser() if a.korpus else task_set / "korpus"
    out = Path(a.out).expanduser()
    docs = load_corpus(korpus)
    for d in docs.values():
        p = out / (d.path + ".txt")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(d.text, encoding="utf-8")
    print(f"{len(docs)} text copies in {out}")


def docker_command(a, name: str, praca: Path, dom: Path, run: Path, korpus: Path, tekst: Path) -> list[str]:
    return ["docker", "run", "--rm", "--name", name, "--network", a.network,
            "--user", f"{os.getuid()}:{os.getgid()}", "--memory", "16g", "--cpus", "8", "--pids-limit", "1024",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,size=2g",
            "-e", "HOME=/dom", "-e", "DO_NOT_TRACK=1", "-e", "MPLCONFIGDIR=/tmp/mpl",
            "-e", "HAPILOT_EGRESS_LOG=/run/egress.jsonl", "-e", f"HAPILOT_EGRESS_ALLOW={a.allow}",
            "-v", f"{praca}:/praca", "-v", f"{korpus}:/praca/dokumenty:ro", "-v", f"{tekst}:/praca/tekst:ro",
            "-v", f"{dom}:/dom", "-v", f"{run}:/run", "-w", "/praca", a.image,
            "node", "--import", f"{RUNNER}/egress_guard.mjs", f"{RUNNER}/run_task.mjs", "/run/spec.json"]


def run_task(a, task: dict, prompt: str, logdir: Path, parse_answer) -> dict:
    tdir = Path(a.work).expanduser() / task["id"]
    shutil.rmtree(tdir, ignore_errors=True)
    praca, dom, run = tdir / "praca", tdir / "dom", tdir / "run"
    for p in (praca, dom, run):
        p.mkdir(parents=True)
    spec = {"prompt": prompt, "cwd": "/praca", "providerId": "openai-compatible", "modelId": a.model,
            "apiKey": "lokalny", "baseUrl": a.base_url, "timeoutSeconds": a.timeout,
            "tracePath": "/run/trace.jsonl", "resultPath": "/run/result.json"}
    (run / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    name = f"hapilot-v3-{task['id'].lower()}"
    korpus = Path(a.korpus).expanduser() / "dokumenty"
    cmd = docker_command(a, name, praca, dom, run, korpus, Path(a.tekst).expanduser())
    stop = threading.Event()
    watcher = threading.Thread(target=watch_container, args=(name, run / "egress_proc.jsonl", stop), daemon=True)
    t0 = time.time()
    status, error, code = "ok", None, None
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    watcher.start()
    try:
        stdout, stderr = proc.communicate(timeout=a.timeout + GRACE_S)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", name], capture_output=True)
        stdout, stderr = proc.communicate()
        status, error = "timeout", f"container killed after {a.timeout + GRACE_S} s"
    stop.set()
    watcher.join(timeout=10)
    duration = round(time.time() - t0, 2)
    (run / "stdout.txt").write_text(stdout or "", encoding="utf-8")
    (run / "stderr.txt").write_text(stderr or "", encoding="utf-8")
    result = {}
    if (run / "result.json").is_file():
        result = json.loads((run / "result.json").read_text(encoding="utf-8"))
        if result.get("status") == "timeout":
            status, error = "timeout", f"no answer in {a.timeout} s"
        elif result.get("status") != "ok" and status == "ok":
            error = (result.get("error") or "")[:500] or None
    elif status == "ok":
        error = f"runner wrote no result (exit {code})"
    ans_file = praca / "odpowiedz.json"
    answer = ans_file.read_text(encoding="utf-8", errors="replace") if ans_file.is_file() else ""
    egress_node = sum(1 for line in (run / "egress.jsonl").read_text().splitlines()
                      if '"allowed":false' in line.replace(" ", "")) if (run / "egress.jsonl").is_file() else 0
    egress_proc = len((run / "egress_proc.jsonl").read_text().splitlines()) if (run / "egress_proc.jsonl").is_file() else 0
    row = make_row(task["id"], answer, parse_answer, result, status, error, duration, code,
                   summarize_trace(run / "trace.jsonl"), egress_node, egress_proc)
    dest = logdir / task["id"]
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(run, dest)
    if ans_file.is_file():
        shutil.copy2(ans_file, dest / "odpowiedz.json")
    shutil.rmtree(tdir, ignore_errors=True)
    return row


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def cmd_run(a) -> None:
    task_set = Path(a.set).expanduser()
    scoring = load_harness(task_set)
    out = Path(a.out).expanduser()
    (out / "logi").mkdir(parents=True, exist_ok=True)
    res_path = out / "results.jsonl"
    done = {}
    if res_path.exists():
        for line in res_path.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            done[r["task_id"]] = r
    tasks = load_tasks(Path(a.tasks).expanduser())
    if a.only:
        tasks = [t for t in tasks if t["id"] in a.only.split(",")]
    template_path = Path(a.template).expanduser()
    template = json.loads(template_path.read_text(encoding="utf-8"))
    image_id = subprocess.run(["docker", "image", "inspect", a.image, "--format", "{{.Id}}"],
                              capture_output=True, text=True).stdout.strip()
    meta = {"image": a.image, "image_id": image_id, "network": a.network, "egress_allow": a.allow,
            "model": a.model, "base_url": a.base_url, "timeout_s": a.timeout, "tasks": str(Path(a.tasks).expanduser()),
            "n_tasks": len(tasks), "korpus": str(Path(a.korpus).expanduser()), "tekst": str(Path(a.tekst).expanduser()),
            "template": str(template_path), "template_sha256": sha256_file(template_path), "firma": a.firma,
            "dzis": a.dzis, "commit": a.commit, "model_server": a.model_server, "start": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (out / "run.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    for t in tasks:
        if t["id"] in done:
            continue
        prompt = build_prompt(template, t, a.firma, date.fromisoformat(a.dzis))
        row = run_task(a, t, prompt, out / "logi", scoring.parse_answer)
        done[t["id"]] = row
        res_path.write_text("".join(json.dumps(done[k], ensure_ascii=False) + "\n" for k in sorted(done)),
                            encoding="utf-8")
        tools = sum(row["trace"]["tool_calls"].values())
        print(f"[v3] {row['status']:7} {t['id']:4} {row['duration_s']:7.1f}s json={row['answer_json_valid']} "
              f"tools={tools} egress={row['egress_blocked_node']}/{row['egress_public_tcp']} {row['error'] or ''}",
              flush=True)
    ids = {t["id"] for t in tasks}
    print(f"done: {sum(r['answer_json_valid'] for k, r in done.items() if k in ids)}/{len(tasks)} with valid JSON")


def score_rows(tasks: list[dict], rows: dict, scoring, manifest_index: dict) -> tuple[list[dict], dict]:
    graded, by_cat = [], {}
    for t in tasks:
        r = rows.get(t["id"])
        answer_text = (r or {}).get("answer_text") or ""
        s = scoring.score(t, answer_text, manifest_index)
        g = {"task_id": t["id"], "kategoria": t.get("kategoria"), "poziom": t.get("poziom"), "sukces": s["sukces"],
             "wynik": s["wynik"], "json_poprawny": s["json_poprawny"], "status": (r or {}).get("status", "missing"),
             "nieudane": [c["id"] for c in s["sprawdzenia"] if not c["ok"]]}
        graded.append(g)
        c = by_cat.setdefault(t.get("kategoria"), [0, 0])
        c[0] += int(s["sukces"])
        c[1] += 1
    n = len(graded)
    summary = {"tasks": n, "success": sum(g["sukces"] for g in graded),
               "mean_score": round(sum(g["wynik"] for g in graded) / n, 4) if n else 0.0,
               "valid_json": sum(g["json_poprawny"] for g in graded),
               "by_category": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_cat.items())}}
    return graded, summary


def cmd_score(a) -> None:
    task_set = Path(a.set).expanduser()
    scoring = load_harness(task_set)
    out = Path(a.out).expanduser()
    rows = {}
    for line in (out / "results.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        rows[r["task_id"]] = r
    tasks = load_tasks(Path(a.tasks).expanduser())
    graded, summary = score_rows(tasks, rows, scoring, load_manifest(Path(a.korpus).expanduser()))
    (out / "ocena.jsonl").write_text("".join(json.dumps(g, ensure_ascii=False) + "\n" for g in graded), encoding="utf-8")
    durations = sorted(r["duration_s"] for r in rows.values())
    summary["median_s"] = durations[len(durations) // 2] if durations else None
    summary["egress_blocked_node"] = sum(r.get("egress_blocked_node", 0) for r in rows.values())
    summary["egress_public_tcp"] = sum(r.get("egress_public_tcp", 0) for r in rows.values())
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--set", required=True, help="task set root (harness/, korpus/)")
    p.add_argument("--korpus", help="corpus root with dokumenty/ (default <set>/korpus)")
    p.add_argument("--out", required=True, help="where the text copies go")
    r = sub.add_parser("run")
    r.add_argument("--set", required=True)
    r.add_argument("--tasks", required=True, help="directory of task YAML files")
    r.add_argument("--korpus", required=True, help="corpus root with dokumenty/ and manifest.json")
    r.add_argument("--tekst", required=True, help="text copies from `prepare`")
    r.add_argument("--template", required=True, help="prompt template JSON (system, task, workspace_note)")
    r.add_argument("--firma", required=True)
    r.add_argument("--dzis", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--work", default="~/hapilot-v3-data/praca")
    r.add_argument("--image", default="hapilot-agent:0.1")
    r.add_argument("--network", default="hapilot-v3-izolowana")
    r.add_argument("--allow", default="model:8000", help="host:port the runner may reach")
    r.add_argument("--model", default="qwen3.6-27b-q4km")
    r.add_argument("--base-url", default="http://model:8000/v1")
    r.add_argument("--timeout", type=int, default=900)
    r.add_argument("--commit", default="")
    r.add_argument("--model-server", default="", help="model server command line, recorded in run.json")
    r.add_argument("--only", help="comma-separated task ids")
    s = sub.add_parser("score")
    s.add_argument("--set", required=True)
    s.add_argument("--tasks", required=True)
    s.add_argument("--korpus", required=True)
    s.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    {"prepare": prepare, "run": cmd_run, "score": cmd_score}[a.cmd](a)


if __name__ == "__main__":
    main()
