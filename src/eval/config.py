"""Run configuration and tasks for evaluation runs (docs/EVALUATION_INTERFACE.md, §1)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import yaml

from src.config import ConfigError, get_settings

TASK_SCHEMA = "hapilot.task/1"
_TASK_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass
class Task:
    id: str
    instruction: str
    answer_format: Optional[dict] = None
    attachments: list[Path] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _TASK_ID.match(self.id) or self.id in (".", ".."):
            raise ConfigError(f"Invalid task id {self.id!r} (allowed: letters, digits, . _ -; at most 64 characters)")
        if not isinstance(self.instruction, str) or not self.instruction.strip():
            raise ConfigError(f"Task {self.id}: empty instruction")
        if self.answer_format is not None:
            fmt = self.answer_format
            if not isinstance(fmt, dict) or not isinstance(fmt.get("fields"), dict) or not fmt["fields"]:
                raise ConfigError(f"Task {self.id}: answer_format needs a non-empty 'fields' object")
            if "schema" in fmt and not isinstance(fmt["schema"], dict):
                raise ConfigError(f"Task {self.id}: answer_format.schema must be a JSON Schema object")
        self.attachments = [Path(p) for p in self.attachments]
        for p in self.attachments:
            if not p.is_file():
                raise ConfigError(f"Task {self.id}: attachment not found: {p}")


def load_tasks(path: Path) -> list[Task]:
    """Read tasks.jsonl; attachment paths are relative to the tasks file."""
    tasks, seen = [], set()
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path}:{n}: not JSON ({exc})") from exc
        unknown = set(raw) - {"id", "instruction", "answer_format", "attachments", "schema"}
        if unknown:
            raise ConfigError(f"{path}:{n}: unknown fields {sorted(unknown)}")
        attachments = [p if Path(p).is_absolute() else str(path.parent / p) for p in raw.get("attachments", [])]
        task = Task(id=raw.get("id"), instruction=raw.get("instruction"), answer_format=raw.get("answer_format"),
                    attachments=attachments)
        if task.id in seen:
            raise ConfigError(f"{path}:{n}: duplicate task id {task.id}")
        seen.add(task.id)
        tasks.append(task)
    return tasks


@dataclass
class RunConfig:
    corpus_dir: Path
    out_dir: Path
    today: str
    profile: Optional[str] = None
    seed: int = 42
    temperature: float = 0.0
    approval_policy: str = "auto"
    task_timeout_s: float = 900.0
    answer_mode: str = "formatter"
    resume: bool = True
    retry_failed: bool = False
    egress_guard: bool = True
    inline_limit: int = 20_000

    def __post_init__(self) -> None:
        self.corpus_dir, self.out_dir = Path(self.corpus_dir).resolve(), Path(self.out_dir).resolve()
        if not self.corpus_dir.is_dir():
            raise ConfigError(f"corpus_dir is not a directory: {self.corpus_dir}")
        try:
            date.fromisoformat(self.today)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"today must be YYYY-MM-DD, got {self.today!r}") from exc
        self.profile = self.profile or get_settings().default_profile
        get_settings().profile(self.profile)  # raises for an unknown profile
        if self.approval_policy != "auto":
            raise ConfigError("Evaluation runs use approval_policy 'auto' (nobody answers questions during a run)")
        if self.answer_mode not in ("formatter", "parse"):
            raise ConfigError("answer_mode must be 'formatter' or 'parse'")
        if self.task_timeout_s <= 0:
            raise ConfigError("task_timeout_s must be positive")

    def public(self) -> dict:
        return {"profile": self.profile, "today": self.today, "seed": self.seed, "temperature": self.temperature,
                "approval_policy": self.approval_policy, "task_timeout_s": self.task_timeout_s,
                "answer_mode": self.answer_mode, "inline_limit": self.inline_limit}


def load_run_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping")
    return data
