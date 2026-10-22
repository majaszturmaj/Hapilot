"""Signatures in generated documents: a blank place for a handwritten signature, or the user's own scan.

Templates mark where the signature goes with the reserved placeholder ``{{PODPIS_SLOT}}`` (the built-in
templates, the builder's "Podpis" block; authors of LaTeX templates may use it too). It becomes a box of fixed
size — empty, to sign the printed document by hand, or holding the scan — so the layout is the same either way.

Who decides: only the user, in the document form ("Podpis"). The model never chooses it, and without a form
(automatic approval, evaluation runs) the place stays blank. The scan used is always the logged-in user's own.

Scans are private: one per user in ``signatures/<owner>/`` under the data directory, PNG or JPEG (checked by
content, at most 2 MB), never shown to other users — administrators included — and never used in evaluation runs.
For a document the scan is copied into the chat's workspace (the only folder the sandbox sees) as ``podpis_*``,
a name the side panel does not list.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import struct
from pathlib import Path
from typing import Optional

from src.config import get_settings

SLOT = "{{PODPIS_SLOT}}"
SLOT_NAME = "PODPIS_SLOT"
BLANK_LABEL = "Miejsce na podpis odręczny"
SCAN_LABEL = "Skan mojego podpisu"
MAX_BYTES = 2 * 1024 * 1024
BOX = r"\parbox[b][1.5cm][b]{6cm}{\centering %s}"


class SignatureError(ValueError):
    """Refused upload; the message is shown to the user (Polish)."""


def _folder(user: str) -> Path:
    from src.admin import owner_folder

    return get_settings().data_dir / "signatures" / owner_folder(user)


# --------------------------------------------------------------------------- image checks (no image library)

def image_info(data: bytes) -> tuple[str, int, int]:
    """(format, width, height) of a PNG or JPEG, read from its header; SignatureError for anything else."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return "png", width, height
    if data[:3] == b"\xff\xd8\xff":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                break
            marker = data[i + 1]
            length = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height, width = struct.unpack(">HH", data[i + 5:i + 9])
                return "jpg", width, height
            i += 2 + length
    raise SignatureError("To nie jest obraz PNG ani JPEG.")


def check(data: bytes) -> tuple[str, int, int]:
    if not data:
        raise SignatureError("Plik jest pusty.")
    if len(data) > MAX_BYTES:
        raise SignatureError("Plik jest za duży (najwyżej 2 MB).")
    fmt, width, height = image_info(data)
    if not (20 <= width <= 6000 and 10 <= height <= 6000):
        raise SignatureError(f"Nietypowy rozmiar obrazu ({width}×{height} px).")
    return fmt, width, height


# --------------------------------------------------------------------------- storage

def path(user: Optional[str]) -> Optional[Path]:
    if not user:
        return None
    folder = _folder(user)
    for name in ("signature.png", "signature.jpg"):
        if (folder / name).is_file():
            return folder / name
    return None


def info(user: str) -> dict:
    p = path(user)
    if p is None:
        return {"present": False}
    meta_file = p.parent / "meta.json"
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.is_file() else {}
    import base64

    mime = "image/png" if p.suffix == ".png" else "image/jpeg"
    return {"present": True, **meta, "src": f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode("ascii")}


def save(user: str, data: bytes) -> dict:
    from src import admin

    fmt, width, height = check(data)
    folder = _folder(user)
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    (folder / f"signature.{fmt}").write_bytes(data)
    (folder / "meta.json").write_text(json.dumps({"format": fmt, "width": width, "height": height, "bytes": len(data),
                                                  "uploaded_at": admin.now()}), encoding="utf-8")
    admin.audit(user, "signature_uploaded", "podpis", f"{fmt.upper()} {width}×{height} px")
    return info(user)


def delete(user: str) -> dict:
    from src import admin

    folder = _folder(user)
    if folder.exists():
        shutil.rmtree(folder)
        admin.audit(user, "signature_deleted", "podpis")
    return {"present": False}


# --------------------------------------------------------------------------- documents

ADD_URL = "/hapilot/admin#signature"  # the agent panel opens the "Mój podpis" dialog


def form_field(user: Optional[str], latex: str, chosen: Optional[str] = None) -> Optional[dict]:
    """The "Podpis" field of the document form, when the template has a signature place.

    Type "signature": the form shows each option as a picture (public/elements/hitl_form.jsx) — the blank place,
    and the user's scan itself — and links to the upload dialog. The submitted value is the option's label.
    """
    if SLOT not in (latex or ""):
        return None
    options = [{"value": BLANK_LABEL, "title": "Podpiszę odręcznie", "hint": "puste miejsce — do podpisu po wydruku",
                "image": None}]
    scan = info(user) if path(user) else None
    if scan:
        options.append({"value": SCAN_LABEL, "title": "Mój skan", "hint": "Twój podpis wstawiony w dokument", "image": scan["src"]})
    value = SCAN_LABEL if chosen == "scan" and scan else BLANK_LABEL
    return {"id": "_signature", "label": "Podpis", "type": "signature", "value": value, "options": options, "required": True,
            "add_url": ADD_URL, "has_scan": bool(scan)}


def mode_from_form(value: Optional[str]) -> str:
    return "scan" if value == SCAN_LABEL else "blank"


def fill_slot(latex: str, mode: str, user: Optional[str], workspace: Optional[Path]) -> tuple[str, str]:
    """Put the signature box into the template; returns (latex, the mode actually used).

    "scan" needs the user's scan and a workspace to copy it into; otherwise the place stays blank.
    """
    if SLOT not in latex:
        return latex, "none"
    scan = path(user) if mode == "scan" else None
    if scan is None or workspace is None:
        return latex.replace(SLOT, BOX % r"\mbox{}"), "blank"
    data = scan.read_bytes()
    name = f"podpis_{hashlib.sha256(data).hexdigest()[:12]}{scan.suffix}"
    (workspace / name).write_bytes(data)
    image = r"\includegraphics[width=5.6cm,height=1.45cm,keepaspectratio]{%s}" % name
    latex = latex.replace(SLOT, BOX % image)
    if not re.search(r"\\usepackage(\[[^\]]*\])?\{[^}]*\bgraphicx\b[^}]*\}", latex):
        latex = latex.replace("\\begin{document}", "\\usepackage{graphicx}\n\\begin{document}", 1)
    return latex, "scan"
