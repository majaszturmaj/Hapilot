"""Test doubles: a scripted chat model and a fake Docker client (no model, no containers)."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Union

from langchain_core.messages import AIMessage

Reply = Union[str, Callable[[list], str]]


class ScriptedLLM:
    """Chat model stand-in: replies per role, in order; records every call."""

    def __init__(self, role: str, script: "LLMScript"):
        self.role = role
        self.script = script

    def invoke(self, messages):
        self.script.calls.append((self.role, messages))
        queue = self.script.replies.get(self.role) or self.script.replies.get("*")
        if not queue:
            raise AssertionError(f"No scripted reply left for role {self.role!r}")
        reply = queue.pop(0) if len(queue) > 1 or self.script.consume_last else queue[0]
        text = reply(messages) if callable(reply) else reply
        return AIMessage(content=text)


class LLMScript:
    """Replies keyed by role ("*" = any role). The last reply of a role repeats unless consume_last."""

    def __init__(self, consume_last: bool = False):
        self.replies: dict[str, list[Reply]] = {}
        self.calls: list = []
        self.specs: list = []
        self.consume_last = consume_last

    def on(self, role: str, *replies: Reply) -> "LLMScript":
        self.replies.setdefault(role, []).extend(replies)
        return self

    def factory(self, spec, role):
        self.specs.append((role, spec))
        return ScriptedLLM(role, self)

    def prompts_for(self, role: str) -> list[str]:
        return ["\n".join(getattr(m, "content", "") for m in msgs) for r, msgs in self.calls if r == role]


class FakeContainer:
    def __init__(self, exit_code: int, output: str, hang: bool = False):
        self.exit_code, self.output, self.hang = exit_code, output, hang
        self.attrs = {"HostConfig": {"NetworkMode": "none"}}
        self.killed = self.removed = False

    def wait(self, timeout=None):
        if self.hang:
            raise TimeoutError("read timed out")
        return {"StatusCode": self.exit_code}

    def kill(self):
        self.killed = True

    def logs(self, stdout=True, stderr=True):
        return self.output.encode("utf-8")

    def remove(self, force=False):
        self.removed = True


class _Images:
    def __init__(self, available: bool):
        self.available = available
        self.pulled = []

    def get(self, name):
        if not self.available:
            raise LookupError(f"image {name} not found")
        return object()

    def pull(self, name, *args, **kwargs):  # must never be called by Hapilot
        self.pulled.append(name)
        raise AssertionError("Hapilot must not pull images at run time")


class _Containers:
    def __init__(self, client: "FakeDocker"):
        self.client = client

    def run(self, image, command, **kwargs):
        self.client.runs.append({"image": image, "command": command, **kwargs})
        workdir = Path(next(iter(kwargs["volumes"])))
        if self.client.hang:
            return FakeContainer(124, "", hang=True)
        if command[0] == "python3":
            proc = subprocess.run([sys.executable, "-I", command[1]], cwd=workdir, capture_output=True, text=True, timeout=30)
            return FakeContainer(proc.returncode, proc.stdout + proc.stderr)
        if command[:2] == ["sh", "-c"] and "tesseract" in command[2]:
            if self.client.ocr_fails:
                return FakeContainer(1, "tesseract failed on /work/p00001.png")
            for image in sorted(workdir.glob("p*.png")):
                assert image.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
                self.client.ocr_pages.append(image.name)
                (image.with_suffix(".txt")).write_text(self.client.ocr_text + "\n\f", encoding="utf-8")
                words = self.client.ocr_text.split()
                rows = ["level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext",
                        "1\t1\t0\t0\t0\t0\t0\t0\t2480\t3508\t-1\t"]
                rows += [f"5\t1\t1\t1\t1\t{i}\t10\t10\t50\t20\t{90 if i % 2 else 80}\t{w}" for i, w in enumerate(words, 1)]
                (image.with_suffix(".tsv")).write_text("\n".join(rows) + "\n", encoding="utf-8")
            return FakeContainer(0, "")
        if command[:2] == ["sh", "-c"] and "pdflatex" in command[2]:
            tex = re.search(r"(\S+)\.tex", command[2]).group(1)
            if self.client.latex_fails:
                (workdir / f"{tex}.log").write_text("! LaTeX Error: something broke.\nl.12 bad line\n")
                return FakeContainer(1, "")
            (workdir / f"{tex}.pdf").write_bytes(b"%PDF-1.4\n% fake pdf for tests\n")
            (workdir / f"{tex}.aux").write_text("aux")
            return FakeContainer(0, "")
        return FakeContainer(0, "")


class FakeDocker:
    def __init__(self, image_available: bool = True):
        self.images = _Images(image_available)
        self.containers = _Containers(self)
        self.runs: list = []
        self.hang = False
        self.latex_fails = False
        self.ocr_fails = False
        self.ocr_text = "Protokół odbioru zeskanowany: urządzenie przyjęto bez zastrzeżeń."
        self.ocr_pages: list = []
