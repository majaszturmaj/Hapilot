"""Per-conversation workspace of the v3 agent: what its container (image hapilot-agent) sees under /praca.

    host                                       container
    <workspace dir>/<conversation>/praca/   →  /praca             working directory, read-write (scratch files)
    <workspace dir>/<conversation>/praca/wyniki →  /praca/wyniki  files for the user
    HAPILOT_DOCS_DIR                        →  /praca/dokumenty   company documents, read-only
    HAPILOT_ADDED_DOCS_DIR                  →  /praca/dodane      documents added to the knowledge base from the chat,
                                                                  read-only
    text mirror (src/text_mirror.py)        →  /praca/tekst       text of both, read-only: <source path>.txt, and
                                                                  <source path>.<sheet>.csv for spreadsheets
    <workspace dir>/<conversation>/zalaczniki/ → /praca/zalaczniki  this conversation's uploads, read-only, each
                                                                  with a text copy (<name>.txt, a CSV per sheet)

``<workspace dir>`` is HAPILOT_AGENT_WORKSPACE_DIR (default ``<data dir>/agent_workspace``). A source path of the
index maps to /praca/dokumenty/<path>, or to /praca/dodane/<rest> for ``dodane/<rest>``; its text to
/praca/tekst/<path>.txt (``document_path``, ``text_path``). The added documents sit beside /praca/dokumenty, not
inside it: a mount inside a read-only mount needs its mount point to exist there, i.e. a folder created in the
company's documents. The read-only mounts' mount points are made in advance as empty folders inside ``praca/``.
"""

from __future__ import annotations

import csv
import glob
import hashlib
import io
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src import text_mirror
from src.config import get_settings
from src.extractors import is_supported

ROOT = "/praca"
DOCS, ADDED, TEXT, UPLOADS, RESULTS = "dokumenty", "dodane", "tekst", "zalaczniki", "wyniki"
ADDED_PREFIX = "dodane"  # source path prefix of the added documents (src/ingest_data.py ADDED_PREFIX)


class WorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Mount:
    source: Path
    target: str
    read_only: bool

    def docker_arg(self) -> str:
        """Value of ``docker run --mount`` (fields quoted as CSV when a path holds a comma or a quote)."""
        fields = ["type=bind", f"source={self.source}", f"target={self.target}"] + (["readonly"] if self.read_only else [])
        buf = io.StringIO()
        csv.writer(buf, lineterminator="").writerow(fields)
        return buf.getvalue()


def workspace_root() -> Path:
    s = get_settings()
    return s.agent_workspace_root or (s.data_dir / "agent_workspace")


def folder_name(conversation_id: str) -> str:
    """The conversation's folder name: the id itself when it is a plain one (a Chainlit thread id is a UUID), else
    a hash of it."""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", conversation_id or ""):
        return conversation_id
    return "k_" + hashlib.sha256((conversation_id or "").encode("utf-8")).hexdigest()[:32]


def document_path(source: str) -> str:
    """Where the container sees a document of the index, by its source path."""
    if source.startswith(ADDED_PREFIX + "/"):
        return f"{ROOT}/{ADDED}/{source[len(ADDED_PREFIX) + 1:]}"
    return f"{ROOT}/{DOCS}/{source}"


def text_path(source: str) -> str:
    """Where the container sees the text mirror of a document of the index."""
    return f"{ROOT}/{TEXT}/{source}.txt"


def _safe_name(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^\w.\-]+", "_", name, flags=re.UNICODE).strip("._") or "zalacznik"


class Workspace:
    """``settings`` replaces the configured documents, mirror and added documents (an evaluation run's corpus)."""

    def __init__(self, conversation_id: str, root: Optional[Path] = None, settings=None):
        self.conversation_id = conversation_id
        self._settings = settings
        self.dir = Path(root or workspace_root()) / folder_name(conversation_id)
        self.praca = self.dir / "praca"
        self.uploads = self.dir / UPLOADS
        self.results_dir = self.praca / RESULTS

    def prepare(self) -> "Workspace":
        """Create the conversation's folders and the mount points; the documents folder must exist."""
        s = self.settings
        if not Path(s.docs_dir).is_dir():
            raise WorkspaceError(f"documents folder not found: {s.docs_dir}")
        for folder in (self.uploads, self.results_dir, *(self.praca / name for name in (DOCS, ADDED, TEXT, UPLOADS)),
                       Path(s.added_docs_dir), text_mirror.mirror_dir(s.chroma_path, s.collection)):
            folder.mkdir(parents=True, exist_ok=True)
        return self

    @property
    def settings(self):
        return self._settings or get_settings()

    def mounts(self) -> list[Mount]:
        s = self.settings
        return [Mount(self.praca.resolve(), ROOT, False),
                Mount(Path(s.docs_dir).resolve(), f"{ROOT}/{DOCS}", True),
                Mount(Path(s.added_docs_dir).resolve(), f"{ROOT}/{ADDED}", True),
                Mount(text_mirror.mirror_dir(s.chroma_path, s.collection).resolve(), f"{ROOT}/{TEXT}", True),
                Mount(self.uploads.resolve(), f"{ROOT}/{UPLOADS}", True)]

    def docker_args(self) -> list[str]:
        """``docker run`` arguments for the mounts and the working directory."""
        args: list[str] = []
        for mount in self.mounts():
            args += ["--mount", mount.docker_arg()]
        return args + ["--workdir", ROOT]

    def add_upload(self, src: Path, name: Optional[str] = None) -> dict:
        """Copy an uploaded file into the conversation's uploads, with a text copy next to it for a supported type
        (rendered as in the text mirror, OCR included). Returns what the agent is told: container paths and the
        reason when the text could not be read."""
        self.prepare()
        name = _safe_name(name or Path(src).name)
        stem, suffix = Path(name).stem, Path(name).suffix
        target, n = self.uploads / name, 1
        while self._taken(target.name):
            target, n = self.uploads / f"{stem}_{n}{suffix}", n + 1
        shutil.copyfile(src, target)
        data = target.read_bytes()
        info = {"name": target.name, "path": f"{ROOT}/{UPLOADS}/{target.name}", "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "text": [], "error": None}
        if not is_supported(target.name):
            info["error"] = f"no text copy for this file type ({suffix or 'no extension'})"
            return info
        rendered = text_mirror.render_safely(text_mirror.Doc(target, target.name, info["sha256"]))
        info["text"] = [f"{ROOT}/{UPLOADS}/{name}" for name in text_mirror.save(self.uploads, target.name, rendered)]
        info["error"] = rendered.error
        return info

    def _taken(self, name: str) -> bool:
        """The name, or a text copy of a file of that name, is already there."""
        pattern = glob.escape(str(self.uploads / name))
        return bool(glob.glob(pattern) or glob.glob(pattern + ".*"))

    def results(self) -> list[dict]:
        """Files the agent left for the user in /praca/wyniki. Links are skipped: one could point at a file of the
        host, outside the workspace."""
        if not self.results_dir.is_dir():
            return []
        base = self.results_dir.resolve()
        found = []
        for p in sorted(self.results_dir.rglob("*")):
            if p.is_symlink() or not p.is_file():
                continue
            try:
                p.resolve().relative_to(base)  # a folder on the way could be a link
            except ValueError:
                continue
            found.append({"name": p.relative_to(self.results_dir).as_posix(), "path": p, "bytes": p.stat().st_size})
        return found
