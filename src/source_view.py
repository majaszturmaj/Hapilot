"""Data for the UI's side panel: the company documents behind an answer and the files a request changed.

**Sources.** During a run the UI collects every retrieval (``tracing.event("retrieval", ...)`` reaches
the run's ``observer``). Those chunks are exactly the text that went into the model's prompt. For
each document the panel shows the whole document with those chunks highlighted:

- PDF: the original pages, rendered on the server (pypdfium2), with boxes over the chunk text.
  The chunk was extracted by pypdf, the boxes come from pdfium's text layer; both are compared
  without whitespace, so line breaks and spacing differences do not matter. Scanned pages have no
  text layer (their text came from OCR, src/ocr.py): they are shown without boxes and marked as scans.
- other formats (.docx, .eml, .xlsx, .txt, .md): the full extracted text (src/extractors.py, the
  same text the index was built from) with character ranges marked.

A document is *cited* when the answer names it (by path or file name).

**Files.** A snapshot of the session workspace before and after a request gives the files that were
created and modified; a modified text file gets a unified diff against its earlier content.

Nothing here depends on Chainlit; ``src/app.py`` sends the result to the browser.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import struct
import threading
import unicodedata
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.extractors import ExtractionError, Segment, extract

ATTACHMENT_PREFIX = "załącznik:"     # source of chunks from a session's attachment collection
ADDED_PREFIX = "dodane"              # source prefix of documents added from the chat (src/ingest_data.py)
_HEADER = re.compile(r"^\[kolumny: [^\n]*\]\n")  # added to spreadsheet chunks at indexing time
_MIN_PIECE = 12                       # shortest line (without whitespace) matched on its own
TEXT_PREVIEW = (".txt", ".md", ".tex", ".csv", ".json", ".py", ".html", ".xml", ".log")
PREVIEW_LIMIT = 200_000               # bytes of a text file kept for preview and diff
HIDDEN = (re.compile(r"^podpis_[0-9a-f]+\.(png|jpe?g)$"), re.compile(r"^snippet_.*\.py$"), re.compile(r".*\.params\.json$"), re.compile(r".*\.(aux|out)$"),
          re.compile(r"^\."))


# --------------------------------------------------------------------------- collecting chunks during a run

class RunObserver:
    """Receives trace events of one request (any thread) and keeps what the panel needs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.chunks: list[dict] = []          # retrieval chunks in order, each once
        self.read_by_code: list[str] = []     # corpus files that Python code opened

    def __call__(self, type_: str, fields: dict) -> None:
        with self._lock:
            if type_ == "retrieval":
                seen = {(c.get("collection"), c.get("chunk_id")) for c in self.chunks}
                for chunk in fields.get("chunks") or []:
                    if (chunk.get("collection"), chunk.get("chunk_id")) not in seen:
                        self.chunks.append(dict(chunk))
                        seen.add((chunk.get("collection"), chunk.get("chunk_id")))
            elif type_ == "code_data_access":
                self.read_by_code += [f for f in fields.get("files") or [] if f not in self.read_by_code]


# --------------------------------------------------------------------------- matching chunk text

def chunk_body(text: str) -> str:
    """The document text of a chunk (without the column header added to spreadsheet chunks)."""
    return _HEADER.sub("", text or "", count=1)


def _norm(text: str) -> tuple[str, list[int]]:
    """Text without whitespace, soft hyphens and case; plus the original index of every kept character."""
    chars, index = [], []
    for i, ch in enumerate(text):
        if ch.isspace() or ch in "­​﻿":
            continue
        for c in unicodedata.normalize("NFKC", ch).lower():
            chars.append(c)
            index.append(i)
    return "".join(chars), index


def find_ranges(haystack: str, needle: str) -> list[tuple[int, int]]:
    """Character ranges of ``haystack`` that hold ``needle``, ignoring whitespace and case.

    The whole needle is tried first; if it does not occur (e.g. a chunk that spans two pages, or text
    that two extractors read differently), each of its lines that is long enough is matched alone.
    """
    hay, hay_index = _norm(haystack)
    if not hay:
        return []

    def locate(piece: str, start: int = 0) -> Optional[tuple[int, int, int]]:
        p, _ = _norm(piece)
        if not p:
            return None
        pos = hay.find(p, start)
        if pos < 0 and start:
            pos = hay.find(p)
        if pos < 0:
            return None
        return hay_index[pos], hay_index[pos + len(p) - 1] + 1, pos + len(p)

    whole = locate(needle)
    if whole:
        return [whole[:2]]
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for line in re.split(r"\n|(?<=[.!?;])\s+", needle):
        if len(_norm(line)[0]) < _MIN_PIECE:
            continue
        hit = locate(line, cursor)
        if hit:
            ranges.append(hit[:2])
            cursor = hit[2]
    return merge_ranges(ranges)


def merge_ranges(ranges: list[tuple[int, int]], gap: int = 2) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def _segment_key(meta: dict) -> tuple:
    return (meta.get("page"), meta.get("sheet"))


def text_marks(segments: list[Segment], chunks: list[dict]) -> list[dict]:
    """Per segment: its label, text and the marked ranges ``[start, end, chunk_no]`` (chunk_no = position in ``chunks``)."""
    sections = [{"label": _label(s.meta), "page": s.meta.get("page"), "sheet": s.meta.get("sheet"),
                 "text": s.text, "marks": []} for s in segments]
    for no, chunk in enumerate(chunks):
        body = chunk_body(chunk.get("text", ""))
        wanted = (chunk.get("page"), chunk.get("sheet"))
        # chunks are cut per page/sheet at indexing time: look in the chunk's own segment first
        order = sorted(range(len(segments)), key=lambda i: _segment_key(segments[i].meta) != wanted)
        for i in order:
            hits = find_ranges(segments[i].text, body)
            if hits:
                sections[i]["marks"] += [[s, e, no] for s, e in hits]
                break
    for section in sections:
        section["marks"].sort()
    return sections


def _label(meta: dict) -> str:
    if "page" in meta:
        return f"Strona {meta['page']}"
    if "sheet" in meta:
        return f"Arkusz {meta['sheet']}"
    return ""


# --------------------------------------------------------------------------- PDF pages

def pdf_page_sizes(path: Path) -> list[tuple[float, float]]:
    """(width, height) of every page, in points."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        return [pdf.get_page_size(i) for i in range(len(pdf))]
    finally:
        pdf.close()


def pdf_scanned_pages(path: Path) -> set[int]:
    """Pages without a text layer (scans): their text came from OCR, so there is nothing to draw boxes on."""
    import pypdfium2 as pdfium

    from src.ocr import needs_ocr

    pdf = pdfium.PdfDocument(str(path))
    try:
        scanned = set()
        for i in range(len(pdf)):
            page = pdf[i]
            textpage = page.get_textpage()
            if needs_ocr(textpage.get_text_range(0, textpage.count_chars())):
                scanned.add(i + 1)
            textpage.close()
            page.close()
        return scanned
    finally:
        pdf.close()


def pdf_boxes(path: Path, page_no: int, texts: list[tuple[str, int]]) -> list[dict]:
    """Highlight boxes on one PDF page for ``(text, chunk_no)`` pairs, in page fractions (origin top left)."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        page = pdf[page_no - 1]
        width, height = page.get_size()
        textpage = page.get_textpage()
        page_text = textpage.get_text_range(0, textpage.count_chars())
        boxes = []
        for text, no in texts:
            for start, end in find_ranges(page_text, text):
                for i in range(textpage.count_rects(start, end - start)):
                    left, bottom, right, top = textpage.get_rect(i)
                    boxes.append({"x": round(left / width, 5), "y": round(1 - top / height, 5),
                                  "w": round((right - left) / width, 5), "h": round((top - bottom) / height, 5),
                                  "chunk": no})
        textpage.close()
        page.close()
        return boxes
    finally:
        pdf.close()


def render_pdf_page(path: Path, page_no: int, scale: float = 1.6) -> bytes:
    """One PDF page as a PNG (no Pillow needed)."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        page = pdf[page_no - 1]
        bitmap = page.render(scale=scale, rev_byteorder=True)  # RGB(A) byte order
        try:
            data = png(bytes(bitmap.buffer), bitmap.width, bitmap.height, bitmap.stride, bitmap.n_channels)
        finally:
            bitmap.close()
            page.close()
        return data
    finally:
        pdf.close()


def png(buffer: bytes, width: int, height: int, stride: int, channels: int) -> bytes:
    """Encode a top-down RGB/RGBA/grey pixel buffer as PNG."""
    color_type = {1: 0, 3: 2, 4: 6}[channels]
    row = width * channels
    raw = b"".join(b"\x00" + buffer[y * stride:y * stride + row] for y in range(height))

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


# --------------------------------------------------------------------------- documents

def resolve_source(source: str, docs_dir: Path, added_dir: Path, workspace: Optional[Path]) -> Optional[Path]:
    """The file behind a chunk's ``source`` (company document, document added from the chat, or attachment)."""
    if source.startswith(ATTACHMENT_PREFIX):
        base, rel = workspace, source[len(ATTACHMENT_PREFIX):]
    elif source.startswith(ADDED_PREFIX + "/"):
        base, rel = added_dir, source[len(ADDED_PREFIX) + 1:]
    else:
        base, rel = docs_dir, source
    if base is None or not rel:
        return None
    base = base.resolve()
    path = (base / rel).resolve()
    if base != path and base not in path.parents:  # never leave the directory
        return None
    return path if path.is_file() else None


def is_cited(source: str, answer: str) -> bool:
    """The answer names the document by its path or file name."""
    lower = (answer or "").lower()
    name = source[len(ATTACHMENT_PREFIX):] if source.startswith(ATTACHMENT_PREFIX) else source
    return name.lower() in lower or Path(name).name.lower() in lower


def group_chunks(chunks: list[dict]) -> dict[str, list[dict]]:
    """Chunks per source file, in order of first appearance; chunks of a file in document order."""
    groups: dict[str, list[dict]] = {}
    for chunk in chunks:
        if chunk.get("file"):
            groups.setdefault(chunk["file"], []).append(chunk)
    for items in groups.values():
        items.sort(key=lambda c: (c.get("page") or 0, str(c.get("sheet") or ""), _chunk_index(c.get("chunk_id", ""))))
    return groups


def _chunk_index(chunk_id: str) -> int:
    match = re.search(r"#(\d+):", chunk_id or "")
    return int(match.group(1)) if match else 0


@dataclass
class SourceDoc:
    source: str                 # as the index names it (path relative to the documents directory)
    path: Optional[Path]        # the file, if it can be found
    chunks: list[dict]
    cited: bool
    read_by_code: bool = False

    def describe(self, page_url) -> dict:
        """JSON for the panel. ``page_url(path, page_no)`` returns the image URL of a PDF page."""
        base = {"source": self.source, "name": Path(self.source.replace(ATTACHMENT_PREFIX, "")).name,
                "cited": self.cited, "read_by_code": self.read_by_code,
                "chunks": [{"no": i, "page": c.get("page"), "sheet": c.get("sheet"), "score": c.get("score"),
                            "rank": c.get("rank"), "text": chunk_body(c.get("text", ""))} for i, c in enumerate(self.chunks)]}
        if self.path is None:
            return {**base, "kind": "missing"}
        if self.path.suffix.lower() == ".pdf":
            try:
                pages = []
                by_page: dict[int, list[tuple[str, int]]] = {}
                for i, c in enumerate(self.chunks):
                    by_page.setdefault(int(c.get("page") or 1), []).append((chunk_body(c.get("text", "")), i))
                scanned = pdf_scanned_pages(self.path)
                for n, (width, height) in enumerate(pdf_page_sizes(self.path), 1):
                    texts = by_page.get(n, [])  # chunks are cut per page at indexing time
                    boxes = pdf_boxes(self.path, n, texts) if texts and n not in scanned else []
                    pages.append({"n": n, "url": page_url(self.path, n), "ratio": round(height / width, 4), "boxes": boxes,
                                  "scan": n in scanned})
                return {**base, "kind": "pdf", "pages": pages}
            except Exception as exc:  # unreadable for pdfium: fall back to the extracted text
                print(f"⚠️ [PANEL] {self.source}: PDF rendering failed ({type(exc).__name__}: {exc}); showing text")
        try:
            segments = extract(self.path)
        except ExtractionError as exc:
            return {**base, "kind": "missing", "error": str(exc)}
        kind = "table" if self.path.suffix.lower() == ".xlsx" else "text"
        return {**base, "kind": kind, "sections": text_marks(segments, self.chunks)}


def source_docs(observer: RunObserver, answer: str, docs_dir: Path, added_dir: Path,
                workspace: Optional[Path]) -> list[SourceDoc]:
    """Documents whose chunks went into the prompts of this request; cited ones first."""
    docs = []
    groups = group_chunks(observer.chunks)
    for source, chunks in groups.items():
        docs.append(SourceDoc(source, resolve_source(source, docs_dir, added_dir, workspace), chunks,
                              is_cited(source, answer), source in observer.read_by_code))
    for source in observer.read_by_code:
        if source not in groups:
            docs.append(SourceDoc(source, resolve_source(source, docs_dir, added_dir, workspace), [],
                                  is_cited(source, answer), True))
    docs.sort(key=lambda d: not d.cited)  # stable: retrieval order within each group
    return docs


# --------------------------------------------------------------------------- workspace changes

@dataclass
class FileState:
    size: int
    sha256: str
    text: Optional[str] = None  # small text files only (for the diff)


def _hidden(name: str) -> bool:
    return any(p.match(name) for p in HIDDEN)


def snapshot(workspace: Path) -> dict[str, FileState]:
    states = {}
    if not workspace.is_dir():
        return states
    for path in sorted(workspace.iterdir()):
        if not path.is_file() or _hidden(path.name):
            continue
        data = path.read_bytes()
        text = None
        if path.suffix.lower() in TEXT_PREVIEW and len(data) <= PREVIEW_LIMIT:
            text = data.decode("utf-8", errors="replace")
        states[path.name] = FileState(len(data), hashlib.sha256(data).hexdigest(), text)
    return states


@dataclass
class FileChange:
    name: str
    status: str                       # created | modified
    size: int
    preview: Optional[str] = None     # text files
    diff: Optional[str] = None        # modified text files
    extra: dict = field(default_factory=dict)


def changes(before: dict[str, FileState], after: dict[str, FileState]) -> list[FileChange]:
    result = []
    for name, state in after.items():
        old = before.get(name)
        if old is not None and old.sha256 == state.sha256:
            continue
        change = FileChange(name, "created" if old is None else "modified", state.size, preview=state.text)
        if old is not None and old.text is not None and state.text is not None:
            change.diff = "\n".join(difflib.unified_diff(old.text.splitlines(), state.text.splitlines(),
                                                         "przed", "po", lineterm="", n=2))
        result.append(change)
    return result
