"""Evaluation runner: tasks in, results + traces out (docs/EVALUATION_INTERFACE.md).

One process runs many tasks; each task gets a fresh session (workspace, conversation, trace). The
corpus is indexed once per run into a collection named after its fingerprint and reused. Results are
appended to ``results.jsonl`` and flushed after every task, so an interrupted run can be resumed.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import subprocess
import time
import urllib.request
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Iterable, Optional

from langchain_core.messages import AIMessage, HumanMessage

from src import attachments, run_context, tracing
from src.config import PROJECT_ROOT, ConfigError, get_settings
from src.eval import formatter
from src.eval.config import RunConfig, Task

RUN_SCHEMA = "hapilot.run/1"
RESULT_SCHEMA = "hapilot.result/1"
LIBRARIES = ["langgraph", "langchain-core", "langchain-openai", "openai", "chromadb", "sentence-transformers", "torch",
             "transformers", "pypdf", "python-docx", "openpyxl", "jsonschema"]


# --------------------------------------------------------------------------- run metadata helpers

def _git() -> dict:
    def run(*args):
        try:
            return subprocess.run(["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            return ""

    return {"commit": run("rev-parse", "HEAD") or None, "describe": run("describe", "--always", "--dirty", "--tags") or None,
            "dirty": bool(run("status", "--porcelain", "--untracked-files=no"))}


def _libraries() -> dict:
    import platform

    versions = {"python": platform.python_version()}
    for name in LIBRARIES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _gpu() -> Optional[str]:
    if not shutil.which("nvidia-smi"):
        return None
    out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], capture_output=True, text=True).stdout
    return out.strip().splitlines()[0] if out.strip() else None


def _embedding_revision(model: str) -> Optional[str]:
    cache = Path(os.environ.get("HF_HUB_CACHE") or Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub")
    ref = cache / f"models--{model.replace('/', '--')}" / "refs" / "main"
    return ref.read_text().strip() if ref.is_file() else None


def _model_server_info(base_url: str) -> dict:
    """Model files and server version from the model swap proxy (empty if not available)."""
    root = base_url[:-3] if base_url.endswith("/v1") else base_url
    try:
        with urllib.request.urlopen(f"{root}/admin/info", timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return {}


def _sandbox_image_id(name: Optional[str] = None) -> Optional[str]:
    """Docker id of the sandbox image (or of ``name``, e.g. the OCR image); None without Docker or the image."""
    try:
        from src import sandbox

        return getattr(sandbox.docker_client().images.get(name or get_settings().sandbox_image), "id", None)
    except Exception:
        return None


def _prompts_digest() -> str:
    """Hash of every prompt and template file plus the built-in fallback prompts."""
    from src import llm_config

    h = hashlib.sha256()
    for path in sorted(list((PROJECT_ROOT / "extensions").rglob("prompts/*.txt")) + list((PROJECT_ROOT / "templates").glob("*"))):
        h.update(path.relative_to(PROJECT_ROOT).as_posix().encode())
        h.update(path.read_bytes())
    h.update(json.dumps(llm_config.FALLBACK_ROLE_PROMPTS, sort_keys=True, ensure_ascii=False).encode())
    h.update((llm_config.SECURITY_RULES + llm_config.FALLBACK_PROJECT_CONTEXT).encode())
    return h.hexdigest()


def index_corpus(corpus_dir: Path) -> dict:
    """Index the corpus once into an isolated collection named after its fingerprint (tests replace this)."""
    from src.ingest_data import corpus_files, corpus_fingerprint, ingest

    fingerprint = corpus_fingerprint(corpus_files(corpus_dir), corpus_dir)
    collection = f"eval_{fingerprint[:12]}"
    path = get_settings().data_dir / "eval_index"
    stats = ingest(corpus_dir, collection, path=path)
    return {"fingerprint": f"sha256:{fingerprint}", "files": stats.get("files", 0), "chunks": stats.get("chunks", 0),
            "collection": collection, "path": str(path), "skipped": stats.get("skipped", [])}


# --------------------------------------------------------------------------- session

class EvalSession:
    def __init__(self, config: RunConfig):
        self.cfg = config
        self.out = config.out_dir
        self.meta: dict = {}
        self.done: dict[str, dict] = {}
        self.app = None
        self.corpus: dict = {}
        self.corpus_files: list[str] = []

    # -- lifecycle
    def __enter__(self) -> "EvalSession":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.finish()

    def start(self) -> None:
        from src.ingest_data import corpus_files
        from src.privacy import apply_offline_env, install_egress_guard

        apply_offline_env()
        for sub in ("traces", "blobs", "workspaces"):
            (self.out / sub).mkdir(parents=True, exist_ok=True)
        if self.cfg.egress_guard:
            install_egress_guard(self.out / "egress.jsonl")
        (self.out / "egress.jsonl").touch()

        settings = get_settings()
        profile = settings.profile(self.cfg.profile)
        self.corpus = index_corpus(self.cfg.corpus_dir)
        self.corpus_files = [p.relative_to(self.cfg.corpus_dir).as_posix() for p in corpus_files(self.cfg.corpus_dir)]
        from src import ocr
        from src.knowledge_base import INDEX_VERSION, RRF_K
        from src.ingest_data import CHUNK_OVERLAP, CHUNK_SIZE

        retrieval = {"method": "hybrid", "k": settings.rag_top_k, "rrf_k": RRF_K, "index_version": INDEX_VERSION}
        chunking = {"size": CHUNK_SIZE, "overlap": CHUNK_OVERLAP}
        hashed = {"run": self.cfg.public(), "profile": {"name": profile.name, "models": {k: m.__dict__ for k, m in profile.models.items()},
                                                         "roles": profile.roles},
                  "retrieval": retrieval, "chunking": chunking, "embedding_model": settings.embedding_model,
                  "sandbox_image": settings.sandbox_image, "sandbox_image_id": _sandbox_image_id(),
                  "ocr": ocr.signature(), "ocr_image_id": _sandbox_image_id(settings.ocr_image) if settings.ocr_enabled else None,
                  "prompts": _prompts_digest(), "corpus": self.corpus["fingerprint"]}
        config_hash = "sha256:" + hashlib.sha256(json.dumps(hashed, sort_keys=True, default=str).encode()).hexdigest()

        previous = json.loads((self.out / "run.json").read_text()) if (self.out / "run.json").is_file() else None
        git = _git()
        if previous and previous.get("config", {}).get("config_hash") != config_hash:
            raise ConfigError(f"{self.out} holds a run with a different configuration "
                              f"({previous.get('config', {}).get('config_hash')}); use another output directory")
        if previous and previous.get("hapilot", {}).get("commit") != git["commit"]:
            raise ConfigError(f"{self.out} holds a run made with Hapilot {previous.get('hapilot', {}).get('describe')}; "
                              "results of two code versions are never mixed — use another output directory")
        server = _model_server_info(profile.default_model().base_url)
        served = server.get("models", {})
        models = []
        for m in profile.models.values():
            info = served.get(m.model, {})
            models.append({"name": m.model, "file": info.get("file"), "bytes": info.get("bytes"), "server": server.get("server")})
        self.meta = {
            "schema": RUN_SCHEMA,
            "run_id": (previous or {}).get("run_id") or f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}",
            "started": (previous or {}).get("started") or datetime.now().astimezone().isoformat(timespec="seconds"),
            "ended": None,
            "hapilot": git,
            "config": {**self.cfg.public(), "config_hash": config_hash, "retrieval": retrieval, "chunking": chunking},
            "models": models,
            "embedding_model": {"name": settings.embedding_model, "revision": _embedding_revision(settings.embedding_model)},
            "corpus": {k: v for k, v in self.corpus.items() if k != "path"},
            "libraries": _libraries(),
            "gpu": _gpu(),
            "egress_guard": self.cfg.egress_guard,
            "tasks": {"total": 0, "ok": 0, "error": 0, "timeout": 0},
        }
        if (self.out / "results.jsonl").is_file():
            for line in (self.out / "results.jsonl").read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    self.done[rec["task_id"]] = rec
        self._write_meta()

        from src.agent_graph import get_app

        self.app = get_app()

    def finish(self) -> None:
        counts = {"total": len(self.done), "ok": 0, "error": 0, "timeout": 0}
        for rec in self.done.values():
            counts[rec["status"]] = counts.get(rec["status"], 0) + 1
        self.meta["tasks"] = counts
        self.meta["ended"] = datetime.now().astimezone().isoformat(timespec="seconds")
        self._write_meta()

    def _write_meta(self) -> None:
        tmp = self.out / "run.json.tmp"
        tmp.write_text(json.dumps(self.meta, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.out / "run.json")

    # -- tasks
    def run_batch(self, tasks: Iterable[Task]) -> dict:
        for task in tasks:
            self.run_task(task)
        return self.meta["tasks"]

    def _should_skip(self, task: Task) -> bool:
        rec = self.done.get(task.id)
        if rec is None or not self.cfg.resume:
            return False
        return not (self.cfg.retry_failed and rec["status"] != "ok")

    def run_task(self, task: Task) -> dict:
        if self._should_skip(task):
            print(f"[eval] skip {task.id} (already {self.done[task.id]['status']})", flush=True)
            return self.done[task.id]
        run_id = self.meta["run_id"]
        workspace_root = self.out / "workspaces"
        trace_path = self.out / "traces" / f"{task.id}.jsonl"
        trace_path.unlink(missing_ok=True)
        shutil.rmtree(workspace_root / task.id, ignore_errors=True)
        recorder = tracing.TraceRecorder(trace_path, self.out / "blobs", run_id, task.id)
        started = time.monotonic()
        deadline = started + self.cfg.task_timeout_s
        context = {"session_id": task.id, "docs_dir": str(self.cfg.corpus_dir), "profile": self.cfg.profile,
                   "approval_policy": self.cfg.approval_policy,
                   "today": self.cfg.today, "trace": recorder, "deadline": deadline, "temperature": self.cfg.temperature,
                   "seed": self.cfg.seed, "workspace_root": str(workspace_root), "kb_path": self.corpus.get("path"),
                   "kb_collection": self.corpus.get("collection")}
        status, error, answer_text, state = "ok", None, "", {}
        fmt_result = {"answer_json": None, "answer_json_valid": None, "answer_json_error": None}
        attached = attachments.PreparedAttachments()
        with run_context.use_context(**context):
            recorder.emit("task_start", session_id=task.id, instruction=task.instruction, answer_format=task.answer_format,
                          attachments=[{"name": p.name, "bytes": p.stat().st_size,
                                        "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in task.attachments])
            try:
                attached = attachments.prepare(task.attachments, index_path=self.out / "attachments_index",
                                               inline_limit=self.cfg.inline_limit)
                text = task.instruction
                if task.answer_format:
                    text += formatter.format_block(task.answer_format)
                text += attached.message_block
                inputs = {"messages": [HumanMessage(content=text)], "next_step": "", "execution_status": "",
                          "retry_count": 0, "task_plan": {}, "task_id": "", "is_complex_task": False, "current_step": None,
                          "generated_code": None, "last_generated_file": "", "hitl_request": None, "hitl_response": None,
                          "halt": False}
                config = run_context.make_config(
                    task.id, profile=self.cfg.profile, approval_policy=self.cfg.approval_policy, today=self.cfg.today,
                    trace=recorder, deadline=deadline, temperature=self.cfg.temperature, seed=self.cfg.seed,
                    workspace_root=str(workspace_root), kb_path=self.corpus.get("path"), docs_dir=str(self.cfg.corpus_dir),
                    kb_collection=self.corpus.get("collection"),
                    extra_collections=[list(attached.collection)] if attached.collection else None)
                state = self.app.invoke(inputs, config=config)
                answer_text = state["messages"][-1].content if state.get("messages") else ""
                if task.answer_format:
                    fmt_result = formatter.produce(task.instruction, task.answer_format, answer_text,
                                                   _tool_context(state.get("messages", [])), self.cfg.answer_mode)
            except tracing.TaskTimeout as exc:
                status, error = "timeout", str(exc)
            except Exception as exc:  # report and go on with the next task
                status, error = "error", f"{type(exc).__name__}: {exc}"

            workspace = workspace_root / task.id
            files_created = []
            for path in sorted(p for p in workspace.rglob("*") if p.is_file()) if workspace.is_dir() else []:
                rel = path.relative_to(workspace).as_posix()
                if rel in attached.names:
                    continue
                data = path.read_bytes()
                files_created.append({"path": rel, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
                recorder.emit("file_created", **files_created[-1])
            duration = round(time.monotonic() - started, 2)
            sources_used = list(recorder.sources) + [n for n in attached.names if n not in recorder.sources]
            result = {"schema": RESULT_SCHEMA, "run_id": run_id, "task_id": task.id, "status": status, "error": error,
                      "answer_text": answer_text, "answer_json": fmt_result["answer_json"],
                      "answer_json_valid": fmt_result["answer_json_valid"], "answer_json_error": fmt_result["answer_json_error"],
                      "sources_used": sources_used, "sources_cited": _cited(fmt_result["answer_json"], self.corpus_files),
                      "files_created": files_created, "plan_tools": recorder.plan_tools, "duration_s": duration,
                      "llm_calls": recorder.llm_calls, "prompt_tokens": recorder.prompt_tokens,
                      "completion_tokens": recorder.completion_tokens, "trace": f"traces/{task.id}.jsonl"}
            recorder.emit("task_end", status=status, error=error, duration_s=duration, llm_calls=recorder.llm_calls,
                          prompt_tokens=recorder.prompt_tokens, completion_tokens=recorder.completion_tokens)
        recorder.close()
        self._append_result(result)
        self.done[task.id] = result
        print(f"[eval] {status:7s} {task.id} {duration:7.1f}s  llm={recorder.llm_calls}" + (f"  {error}" if error else ""), flush=True)
        return result

    def _append_result(self, result: dict) -> None:
        path = self.out / "results.jsonl"
        lines = []
        if path.is_file():  # a retried task replaces its earlier line
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines()
                     if ln.strip() and json.loads(ln)["task_id"] != result["task_id"]]
        lines.append(json.dumps(result, ensure_ascii=False))
        tmp = path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)


def _tool_context(messages: list) -> str:
    """Tool results and intermediate answers of the task, for the answer formatter."""
    parts = []
    for m in messages[1:]:
        content = getattr(m, "content", "")
        if isinstance(m, AIMessage) or (isinstance(m, HumanMessage) and content.startswith("WYNIK:")):
            parts.append(content)
    return "\n---\n".join(parts)


def _cited(answer_json: Optional[dict], corpus_files: list[str]) -> list[str]:
    """Corpus files named in list-valued fields of the JSON answer (matched by path or base name)."""
    if not isinstance(answer_json, dict):
        return []
    by_name = {}
    for rel in corpus_files:
        by_name.setdefault(Path(rel).name.lower(), rel)
        by_name[rel.lower()] = rel
    cited = []
    for value in answer_json.values():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    rel = by_name.get(item.strip().lower()) or by_name.get(Path(item.strip()).name.lower())
                    if rel and rel not in cited:
                        cited.append(rel)
    return cited
