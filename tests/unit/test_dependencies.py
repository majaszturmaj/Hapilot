"""requirements.txt lists only what the code uses (plus pins that are named with a reason)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = {"langchain-openai": "langchain_openai", "langchain-core": "langchain_core", "langchain-text-splitters":
          "langchain_text_splitters", "langchain-huggingface": "langchain_huggingface", "langchain-chroma": "langchain_chroma",
          "python-docx": "docx", "python-dotenv": "dotenv", "PyYAML": "yaml"}
# pinned on purpose although not imported directly
INDIRECT = {"openai": "langchain-openai's client, pinned for a known-good pair",
            "pydantic": "used by langgraph/chainlit, pinned for a known-good set",
            "sentence-transformers": "the E5 embedding backend of langchain-huggingface",
            "torch": "sentence-transformers' runtime (CPU build, see the comment at the top)"}


def test_every_direct_requirement_is_imported_or_explained():
    names = [re.split(r"[=<>\[]", line)[0].strip() for line in (ROOT / "requirements.txt").read_text().splitlines()
             if line.strip() and not line.startswith("#")]
    code = "\n".join(p.read_text(encoding="utf-8") for d in ("src", "extensions", "scripts", "tools")
                     for p in (ROOT / d).rglob("*.py"))
    unused = []
    for name in names:
        module = MODULE.get(name, name.replace("-", "_"))
        if name not in INDIRECT and not re.search(rf"^\s*(?:import|from)\s+{module}\b", code, re.MULTILINE):
            unused.append(name)
    assert unused == []
