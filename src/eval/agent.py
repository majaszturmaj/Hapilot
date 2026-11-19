"""Evaluation runs on the v3 engine (``--engine v3``): the same interface as v2 (docs/EVALUATION_INTERFACE.md), but each
task runs in a fresh agent container (image hapilot-agent) on the Cline SDK, in the measured headless mode.

    task ──► prompt: "Dziś jest …", the instruction, the answer format, the attachments, where the answer goes
         ──► docker run hapilot-agent … run_task.mjs  (mode yolo, Hapilot's rules and domain tools)
                │ the host bridge (src/agent_bridge.py): search in the run's own index, PDF templates, the answer check
                ▼
         run folder: spec, the runner's full trace (prompts, model text, every tool call), bridge log, egress log
         ──► hapilot.result/1 line and hapilot.trace/1 events (llm_call, tool_call, retrieval, approval,
             answer_format, file_created)

The answer: with an answer format the agent writes one JSON object to /praca/odpowiedz.json and calls submit_and_exit;
the bridge checks it (fields, the task's schema) and an invalid answer is refused once with the reason, so the agent
corrects it in the same run (agent/runner/answer.mjs). Without a format the answer is the submit summary.

What maps how (v2 → v3):
- ``plan_tools``: v3 has no plan; the tools the agent called, in order of first call.
- ``sources_used``: corpus files whose text reached the model: chunks returned by ``semantic_search``, files read
  with ``read_files``, files named in shell commands, files listed in ``search_codebase`` output; plus attachments.
- ``llm_calls`` and tokens: the SDK's per-call usage; the full texts are in the runner's own trace
  (``workspaces/<task>/run/trace.jsonl``), which the ``llm_call`` events do not copy into blobs.
- e-mail drafts are off; approvals refused (nobody answers during a run).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Optional

from src.agent_bridge import Bridge, deny_all
from src.agent_engine import BRIDGE_DIR, GRACE_S, SOCKET, AgentEngine, Turn, polish_date
from src.agent_workspace import DOCS, ROOT, TEXT, UPLOADS, Workspace
from src.config import ConfigError, Settings, get_settings, load_agent_profiles

ANSWER_FILE = "odpowiedz.json"
CORRECTIONS = 1
SUMMARY_CHARS = 500
READING_TOOLS = ("read_files", "run_commands", "search_codebase")
RUNNER_SDK = "@cline/sdk 0.2.0"


def agent_model(profile: Optional[str]) -> tuple[str, str]:
    """(profile name, model name) of a v3 agent profile (config/agent_profiles.yaml)."""
    profiles, default = load_agent_profiles()
    name = profile or default
    if name not in profiles:
        raise ConfigError(f"Unknown agent profile {name!r}; defined: {', '.join(profiles)}")
    return name, profiles[name]["model"]


def task_prompt(instruction: str, answer_format: Optional[dict], uploads: list, today: str) -> str:
    """What the agent gets for an evaluation task (the same for every task; nothing depends on its id)."""
    from src.eval import formatter

    lines = [f"Dziś jest {polish_date(date.fromisoformat(today))}.", "", instruction.strip()]
    text = "\n".join(lines)
    if answer_format:
        text += formatter.format_block(answer_format)
    if uploads:
        text += f"\n\nZałączniki użytkownika (tylko do odczytu, w {ROOT}/{UPLOADS}):"
        for u in uploads:
            note = f"\n- {u['path']}"
            if u.get("text"):
                note += " (tekst: " + ", ".join(u["text"]) + ")"
            if u.get("error"):
                note += f" — {u['error']}"
            text += note
    if answer_format:
        text += (f"\n\nOdpowiedź końcową zapisz jako jeden obiekt JSON w tym formacie w pliku {ROOT}/{ANSWER_FILE}, "
                 "a potem wywołaj submit_and_exit.")
    else:
        text += "\n\nOdpowiedź końcową podaj w submit_and_exit (pole summary)."
    return text


def image_id(image: str) -> Optional[str]:
    try:
        out = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image], capture_output=True,
                             text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def sha256_file(path: Optional[Path]) -> Optional[str]:
    if not path or not Path(path).is_file():
        return None
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class AgentTaskRunner:
    """Runs evaluation tasks in agent containers against the run's corpus index (one task at a time)."""

    def __init__(self, *, corpus_dir: Path, corpus: dict, corpus_files: list[str], out_dir: Path, profile: Optional[str],
                 timeout_s: float, settings: Optional[Settings] = None, command=None):
        base = settings or get_settings()
        self.profile, model = agent_model(profile)
        self.out = out_dir
        self.corpus_files = corpus_files
        self.empty_added = out_dir / "dodane_puste"  # the run has no added documents; the mount point needs a folder
        self.empty_added.mkdir(parents=True, exist_ok=True)
        self.settings = dataclasses.replace(
            base, docs_dir=Path(corpus_dir), chroma_path=Path(corpus["path"]), collection=corpus["collection"],
            added_docs_dir=self.empty_added, agent_model=model, agent_email=False, agent_timeout=int(timeout_s))
        self.engine = AgentEngine(self.settings)
        self.command = command or self.engine.container_command
        self.targets = [(Path(corpus["path"]), corpus["collection"])]

    def describe(self) -> dict:
        """What run.json records about the engine (and what the configuration hash covers)."""
        s = self.settings
        return {"engine": "v3", "profile": self.profile, "model": s.agent_model, "model_url": s.agent_model_url,
                "image": s.agent_image, "image_id": image_id(s.agent_image), "network": s.agent_network,
                "rules_sha256": sha256_file(s.agent_rules), "mode": "yolo", "answer_corrections": CORRECTIONS,
                "sdk": RUNNER_SDK}

    def spec(self, prompt: str, answer_format: Optional[dict]) -> dict:
        spec = self.engine.spec(Turn(chat_id="eval", prompt=prompt))
        for key in ("events", "initialMessagesPath", "messagesPath", "ask"):
            spec.pop(key, None)
        spec["mode"] = "yolo"
        spec["approval"] = {"ask": [], "bridge": f"{BRIDGE_DIR}/{SOCKET}"}
        if answer_format:
            spec["answer"] = {"bridge": f"{BRIDGE_DIR}/{SOCKET}", "file": f"{ROOT}/{ANSWER_FILE}",
                              "corrections": CORRECTIONS}
        return spec

    # ------------------------------------------------------------------ one task

    def run(self, task, today: str, recorder, deadline_s: float) -> dict:
        """Run one task; emit its trace events; return the fields of its result line (without run/task ids)."""
        ws_root = self.out / "workspaces"
        shutil.rmtree(ws_root / task.id, ignore_errors=True)
        ws = Workspace(task.id, root=ws_root, settings=self.settings).prepare()
        uploads = [ws.add_upload(p) for p in task.attachments]
        run, dom = ws.dir / "run", ws.dir / "dom"
        run.mkdir(parents=True, exist_ok=True)
        dom.mkdir(exist_ok=True)
        prompt = task_prompt(task.instruction, task.answer_format, uploads, today)
        (run / "spec.json").write_text(json.dumps(self.spec(prompt, task.answer_format), ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        bridge = Bridge(ws, email=False, approver=deny_all, trace_path=run / "bridge.jsonl",
                        search_targets=self.targets, answer_format=task.answer_format, record_chunks=True)
        sock_dir = Path(tempfile.mkdtemp(prefix="hb-"))
        name = f"hapilot-v3-eval-{re.sub(r'[^A-Za-z0-9_.-]', '_', task.id)[:40]}-{int(time.time())}"
        status, error = None, None
        try:
            with bridge.serve(sock_dir / SOCKET):
                cmd = self.command(ws, run, dom, sock_dir, name)
                try:
                    proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True,
                                          timeout=max(1.0, deadline_s) + GRACE_S)
                    (run / "stderr.txt").write_bytes(proc.stderr[-200_000:])
                except subprocess.TimeoutExpired:
                    status, error = "timeout", f"time budget exhausted ({deadline_s:.0f} s)"
                    try:
                        subprocess.run(["docker", "kill", name], capture_output=True, timeout=60)
                    except (OSError, subprocess.SubprocessError):
                        pass
        finally:
            shutil.rmtree(sock_dir, ignore_errors=True)
        data = _read_json(run / "result.json") or {}
        if status is None:
            runner_status = data.get("status")
            status = {"ok": "ok", "timeout": "timeout"}.get(runner_status, "error")
            if status != "ok":
                error = (str(data.get("error") or "") or ("runner wrote no result" if not data else
                                                          f"finish reason {data.get('finish_reason')}"))[:1000]
        events = _read_jsonl(run / "trace.jsonl")
        summary = self._emit_trace(events, bridge, recorder)
        answer = self._answer(task, data, bridge, ws)
        files = self._files(ws, recorder)
        egress = [e for e in _read_jsonl(run / "egress.jsonl") if not e.get("allowed", False)]
        if egress:
            with open(self.out / "egress.jsonl", "a", encoding="utf-8") as fh:
                for e in egress:
                    fh.write(json.dumps({"task_id": task.id, "source": "agent container", **e}, ensure_ascii=False) + "\n")
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        sources = summary["sources"] + [u["name"] for u in uploads if u["name"] not in summary["sources"]]
        return {"status": status, "error": error, **answer, "sources_used": sources,
                "sources_cited": [], "files_created": files, "plan_tools": summary["tools"],
                "llm_calls": summary["llm_calls"],
                "prompt_tokens": int(usage.get("inputTokens") or summary["prompt_tokens"]),
                "completion_tokens": int(usage.get("outputTokens") or summary["completion_tokens"]),
                "agent": {"status": data.get("status"), "finish_reason": data.get("finish_reason"),
                          "iterations": data.get("iterations"), "tool_calls": summary["tool_counts"],
                          "answer_checks": len(bridge.answers), "egress_blocked": len(egress),
                          "run": (run.relative_to(self.out)).as_posix()}}

    # ------------------------------------------------------------------ result fields

    def _answer(self, task, data: dict, bridge: Bridge, ws: Workspace) -> dict:
        submitted = data.get("submitted") if isinstance(data.get("submitted"), dict) else {}
        text = str(submitted.get("summary") or data.get("text") or "")
        out = {"answer_text": text, "answer_json": None, "answer_json_valid": None, "answer_json_error": None}
        if not task.answer_format:
            return out
        if bridge.answers:  # the last check at submit_and_exit
            last = bridge.answers[-1]
            out.update(answer_json=last["json"], answer_json_valid=last["valid"], answer_json_error=last["error"])
            return out
        # no submit (error, timeout): the answer file as the agent left it, checked here without a correction
        from src.core.tools_common import extract_json_object
        from src.eval.formatter import check

        path = ws.praca / ANSWER_FILE
        raw = path.read_text(encoding="utf-8", errors="replace") if path.is_file() and not path.is_symlink() else ""
        obj = extract_json_object(raw) if raw.strip() else None
        error = check(obj, task.answer_format)
        out.update(answer_json=obj if isinstance(obj, dict) else None, answer_json_valid=error is None,
                   answer_json_error=error)
        return out

    def _files(self, ws: Workspace, recorder) -> list[dict]:
        """Files the agent left in /praca (the mount points of the read-only folders are empty on the host)."""
        files = []
        base = ws.praca.resolve()
        for path in sorted(p for p in ws.praca.rglob("*") if p.is_file() and not p.is_symlink()):
            try:
                path.resolve().relative_to(base)
            except ValueError:
                continue
            data = path.read_bytes()
            entry = {"path": path.relative_to(ws.praca).as_posix(), "bytes": len(data),
                     "sha256": hashlib.sha256(data).hexdigest()}
            files.append(entry)
            recorder.emit("file_created", **entry)
        return files

    # ------------------------------------------------------------------ trace

    def _emit_trace(self, events: list[dict], bridge: Bridge, recorder) -> dict:
        """hapilot.trace/1 events from the runner's trace and the bridge's searches; the result's tool fields."""
        starts: dict = {}
        tools: list[str] = []
        counts: dict[str, int] = {}
        sources: list[str] = []
        retrievals = list(bridge.retrievals)
        n_calls = prompt_tokens = completion_tokens = 0

        def add_source(rel: Optional[str]) -> None:
            if rel and rel not in sources:
                sources.append(rel)

        for e in events:
            kind = e.get("type")
            if kind == "usage":
                n_calls += 1
                prompt_tokens += int(e.get("in") or 0)
                completion_tokens += int(e.get("out") or 0)
                recorder.emit("llm_call", call_id=f"{recorder.task_id}-{n_calls}", node="agent", role="agent",
                              model=self.settings.agent_model, base_url=self.settings.agent_model_url,
                              params={"mode": "yolo"}, messages_ref=None, response_ref=None,
                              prompt_tokens=int(e.get("in") or 0), completion_tokens=int(e.get("out") or 0),
                              latency_ms=None, finish_reason=None, error=None, t_ms=e.get("t"))
            elif kind == "tool_start":
                starts[e.get("id")] = e.get("input")
            elif kind == "tool_end":
                name = str(e.get("tool") or "")
                if name not in tools:
                    tools.append(name)
                counts[name] = counts.get(name, 0) + 1
                args = starts.pop(e.get("id"), None)
                output = e.get("output")
                if name == "semantic_search" and retrievals:
                    r = retrievals.pop(0)
                    recorder.emit("retrieval", query=r["query"], method="hybrid", k=r["k"], chunks=r["chunks"],
                                  duration_ms=r["ms"])
                    for chunk in r["chunks"]:
                        add_source(chunk.get("file"))
                if name in READING_TOOLS:
                    texts = [_as_text(args)] + ([_as_text(output)] if name == "search_codebase" else [])
                    for rel in self._mentioned(texts):
                        add_source(rel)
                recorder.emit("tool_call", name=name, depth=0, args=_compact(args, recorder),
                              result_summary=None if output is None else _as_text(output)[:SUMMARY_CHARS],
                              duration_ms=e.get("ms"), exception=e.get("error"), t_ms=e.get("t"))
            elif kind == "approval":
                recorder.emit("approval", kind=e.get("tool"), policy="auto",
                              decision="approved" if e.get("approved") else "denied", summary=e.get("reason"),
                              t_ms=e.get("t"))
            elif kind == "answer_check":
                recorder.emit("answer_format", mode="submit", valid=e.get("valid"), error=e.get("error"),
                              attempts=e.get("attempt"), accepted=e.get("accepted"), t_ms=e.get("t"))
        recorder.llm_calls, recorder.prompt_tokens, recorder.completion_tokens = n_calls, prompt_tokens, completion_tokens
        return {"tools": tools, "tool_counts": counts, "sources": sources, "llm_calls": n_calls,
                "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}

    def _mentioned(self, texts: list[str]) -> list[str]:
        """Corpus files named by their container path (original or text copy) in the given texts."""
        joined = "\n".join(t for t in texts if t)
        found = []
        for rel in self.corpus_files:
            if f"{ROOT}/{DOCS}/{rel}" in joined or f"{ROOT}/{TEXT}/{rel}." in joined \
                    or f"{DOCS}/{rel}" in joined or f"{TEXT}/{rel}." in joined:
                found.append(rel)
        return found


# --------------------------------------------------------------------------- helpers

def _as_text(value) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _compact(value, recorder, limit: int = 500):
    """Tool arguments for the trace: small values inline, long strings as blobs of the run (as v2's tracing does)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            pass

    def walk(v):
        if isinstance(v, str):
            return {"blob": recorder.blob({"text": v}), "chars": len(v)} if len(v) > limit else v
        if isinstance(v, dict):
            return {str(k): walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v if isinstance(v, (int, float, bool)) or v is None else repr(v)[:200]

    return walk(value)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out
