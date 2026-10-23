"""The company look of the chat UI: name, a short company description, logo and main colour.

An administrator sets them in the agent panel (tab "🎨 Wygląd", src/ui_admin.py); they apply to every user at
once, without a restart:

- **name** — page title, login page, the assistant's name on messages and in the welcome message, the
  "O aplikacji" page, the agent panel; in the system prompts every "Hapilot" (also inflected: "Hapilota",
  "Hapilocie", …) becomes the name, in its base form (Polish endings cannot be derived for any name);
- **description** (1–2 sentences) — added to the system prompts as a ``[FIRMA]`` section, so the agent knows
  where it works; not shown to users;
- **logo** (PNG or JPEG, checked by content; SVG is refused because it can carry scripts) — header, login page,
  favicon and the assistant's avatar;
- **colour** — buttons and accents of the chat and of the agent panel.

Stored in ``<data dir>/branding/``: ``branding.json`` with every version (who, when) and the logos by content
hash, so any version can be restored; every change is in the audit log. UI only: the chat process applies it
(``enable_for_ui``, called by src/app.py); evaluation runs and scripts never do, so they always run as Hapilot.
"""

from __future__ import annotations

import base64
import colorsys
import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Optional

from src.config import get_settings

DEFAULT_NAME = "Hapilot"
DEFAULT_COLOR = "#81ab28"
MAX_NAME = 40
MAX_DESCRIPTION = 300
MAX_LOGO_BYTES = 1024 * 1024
CREDIT = "Działa na [Hapilot](https://github.com/majaszturmaj/Hapilot) — otwartym, lokalnym asystencie AI."

# Hapilot and its Polish forms (Hapilota, Hapilotowi, Hapilotem, Hapilocie, Hapiloty, …)
_NAME_FORMS = re.compile(r"\bHapilo(?:t(?:a|owi|em|y|ów|om|ami|ach)?|cie)\b")
# UI phrases where the declined form would not fit another name: rephrased (translations, src/app.py)
UI_PHRASES = {"Zaloguj się do Hapilota": "Zaloguj się — {name}", "O Hapilocie": "O aplikacji"}
_NAME_OK = re.compile(r"[^\W_][\w .,()+/!?:-]*")  # letters/digits first; nothing HTML could interpret (<>&"')
_COLOR_OK = re.compile(r"#[0-9a-fA-F]{6}")

_lock = threading.RLock()
_enabled = False


class BrandingError(ValueError):
    """Refused change; the message is shown to the administrator (Polish)."""


# --------------------------------------------------------------------------- storage

def folder() -> Path:
    return get_settings().data_dir / "branding"


def _file() -> Path:
    return folder() / "branding.json"


def history() -> list[dict]:
    try:
        return json.loads(_file().read_text(encoding="utf-8")).get("history", [])
    except (OSError, ValueError):
        return []


def _defaults() -> dict:
    return {"name": DEFAULT_NAME, "description": "", "color": DEFAULT_COLOR, "logo": None, "customised": False}


def current() -> dict:
    """The look in force (the defaults when nothing was set or after a reset)."""
    versions = history()
    if not versions or versions[-1].get("reset"):
        return {**_defaults(), **({"updated_at": versions[-1]["ts"], "updated_by": versions[-1]["user"]} if versions else {})}
    last = versions[-1]
    return {"name": last["name"], "description": last.get("description", ""), "color": last.get("color", DEFAULT_COLOR),
            "logo": last.get("logo"), "customised": True, "updated_at": last["ts"], "updated_by": last["user"]}


def _append(entry: dict) -> None:
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"history": history() + [entry]}, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def logo_path() -> Optional[Path]:
    name = current().get("logo")
    path = folder() / "logos" / name if name else None
    return path if path is not None and path.is_file() else None


# --------------------------------------------------------------------------- checks

def check_name(value) -> str:
    name = re.sub(r"\s+", " ", str(value or "")).strip()
    if not 2 <= len(name) <= MAX_NAME:
        raise BrandingError(f"Nazwa musi mieć od 2 do {MAX_NAME} znaków.")
    if not _NAME_OK.fullmatch(name):
        raise BrandingError("Nazwa może zawierać litery, cyfry, spacje i znaki . , ( ) + / ! ? : - (zaczyna się literą "
                            "lub cyfrą).")
    return name


def check_description(value) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > MAX_DESCRIPTION:
        raise BrandingError(f"Opis firmy jest za długi (najwyżej {MAX_DESCRIPTION} znaków, czyli 1–2 zdania).")
    if re.search(r"[<>]", text):
        raise BrandingError("Opis firmy nie może zawierać znaków < ani >.")
    return text


def check_color(value) -> str:
    color = str(value or DEFAULT_COLOR).strip()
    if not _COLOR_OK.fullmatch(color):
        raise BrandingError("Kolor podaj w postaci #RRGGBB.")
    return color.lower()


def check_logo(data: bytes) -> str:
    """Format ("png" or "jpg") of an acceptable logo."""
    from src.signatures import SignatureError, image_info

    if not data:
        raise BrandingError("Plik logo jest pusty.")
    if len(data) > MAX_LOGO_BYTES:
        raise BrandingError("Logo jest za duże (najwyżej 1 MB).")
    try:
        fmt, width, height = image_info(data)
    except SignatureError:
        raise BrandingError("Logo musi być obrazem PNG albo JPEG (SVG nie jest przyjmowany).") from None
    if not (16 <= width <= 4000 and 16 <= height <= 4000):
        raise BrandingError(f"Nietypowy rozmiar logo ({width}×{height} px); dozwolone 16–4000 px.")
    return fmt


# --------------------------------------------------------------------------- changes (administrators only)

def _require_admin(user: str) -> None:
    from src import admin

    if not admin.is_admin(user):
        raise BrandingError("Tylko administrator może zmienić wygląd aplikacji.")


def save(data: dict, user: str) -> dict:
    """Save a new version. ``data``: name, description, color and logo — a data URL / base64 string of a new
    PNG/JPEG, ``"remove"``, or absent to keep the current one."""
    from src import admin

    with _lock:
        _require_admin(user)
        name = check_name(data.get("name"))
        description = check_description(data.get("description"))
        color = check_color(data.get("color"))
        logo = current().get("logo")
        if data.get("logo") == "remove":
            logo = None
        elif data.get("logo"):
            try:
                raw = base64.b64decode(str(data["logo"]).split(",")[-1], validate=True)
            except (ValueError, TypeError):
                raise BrandingError("Nie udało się odczytać pliku logo.") from None
            fmt = check_logo(raw)
            logo = f"logo-{hashlib.sha256(raw).hexdigest()[:16]}.{fmt}"
            target = folder() / "logos" / logo
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_file():
                target.write_bytes(raw)
        before = current()
        if before["customised"] and all(before.get(k) == v for k, v in
                                        (("name", name), ("description", description), ("color", color), ("logo", logo))):
            return info(user)
        _append({"ts": admin.now(), "user": user, "name": name, "description": description, "color": color, "logo": logo})
        changed = [label for key, label, value in (("name", "nazwa", name), ("description", "opis", description),
                                                   ("color", "kolor", color), ("logo", "logo", logo))
                   if before.get(key) != value]
        admin.audit(user, "branding_saved", name, ", ".join(changed) or "bez zmian")
        apply_runtime()
        return info(user)


def reset(user: str) -> dict:
    from src import admin

    with _lock:
        _require_admin(user)
        if current()["customised"]:
            _append({"ts": admin.now(), "user": user, "reset": True})
            admin.audit(user, "branding_reset", DEFAULT_NAME, "przywrócono wygląd Hapilota")
            apply_runtime()
        return info(user)


def restore(index: int, user: str) -> dict:
    """Make an earlier version the current one again (as a new version)."""
    versions = history()
    if not 0 <= index < len(versions) or versions[index].get("reset"):
        raise BrandingError("Nie ma takiej wersji wyglądu.")
    old = versions[index]
    logo = old.get("logo")
    with _lock:
        data = {"name": old["name"], "description": old.get("description", ""), "color": old.get("color")}
        if logo and (folder() / "logos" / logo).is_file():
            data["logo"] = base64.b64encode((folder() / "logos" / logo).read_bytes()).decode("ascii")
        else:
            data["logo"] = "remove"
        return save(data, user)


def info(user: Optional[str] = None) -> dict:
    """The current look for the panel; administrators also get the history."""
    from src import admin

    cur = current()
    result = {**cur, "logo_url": logo_url(), "default": _defaults(), "admin": admin.is_admin(user),
              "limits": {"name": MAX_NAME, "description": MAX_DESCRIPTION, "logo_bytes": MAX_LOGO_BYTES}}
    if admin.is_admin(user):
        result["history"] = [{"index": i, "ts": v["ts"], "user": v["user"], "reset": bool(v.get("reset")),
                              "name": v.get("name"), "description": v.get("description"), "color": v.get("color"),
                              "logo": bool(v.get("logo"))} for i, v in enumerate(history())][::-1]
    return result


def logo_url() -> Optional[str]:
    """Versioned URL of the custom logo (a new upload gets a new URL, so browsers do not show a cached one)."""
    name = current().get("logo")
    return f"/logo?v={name.split('-')[1].split('.')[0]}" if name and logo_path() else None


# --------------------------------------------------------------------------- applying it (the UI process only)

def enable_for_ui() -> None:
    global _enabled
    _enabled = True
    apply_runtime()


def disable() -> None:
    """For tests: back to the state of a process that never enabled branding (evaluation, scripts)."""
    global _enabled
    _enabled = False


def enabled() -> bool:
    return _enabled


def active() -> dict:
    """The look the running process uses: the stored one in the UI, always the defaults elsewhere."""
    return current() if _enabled else _defaults()


def name() -> str:
    return active()["name"]


def apply_runtime() -> None:
    """Push the name into Chainlit's configuration (page title, default avatar); no-op outside the UI."""
    if not _enabled:
        return
    try:
        from chainlit.config import config
    except ImportError:
        return
    config.ui.name = name()


def replace_name(text: str, new: Optional[str] = None) -> str:
    """Every form of "Hapilot" in ``text`` replaced by ``new`` (default: the active name)."""
    new = new if new is not None else name()
    return text if new == DEFAULT_NAME else _NAME_FORMS.sub(lambda m: new, text)


def ui_text(text: str) -> str:
    """A UI string with the active name: fixed phrasings first (declension), then every other form."""
    look = active()
    if not look["customised"]:
        return text
    for phrase, replacement in UI_PHRASES.items():
        text = text.replace(phrase, replacement.format(name=look["name"]))
    return replace_name(text, look["name"])


def apply_to_prompt(system_prompt: str, role: str) -> str:
    """The system prompt with the company's name and description (UI process with a custom look only)."""
    look = active()
    if not look["customised"]:
        return system_prompt
    text = replace_name(system_prompt, look["name"])
    if look["description"] and role != "coder":  # the coder gets no company context (as with the project context)
        text += (f"\n\n[FIRMA]\n{look['description']}\nPrzedstawiając się, używaj nazwy: {look['name']}.\n")
    return text


def markdown(text: str) -> str:
    """The "O aplikacji" page: the name replaced and, with a custom look, the credit to Hapilot."""
    look = active()
    if not look["customised"]:
        return text
    return replace_name(text, look["name"]).rstrip() + "\n\n---\n\n" + CREDIT + "\n"


# --------------------------------------------------------------------------- colour

def _hsl(color: str) -> tuple[int, int, int]:
    r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    h, lightness, s = colorsys.rgb_to_hls(r, g, b)
    return round(h * 360), round(s * 100), round(lightness * 100)


def _hex(h: float, s: float, lightness: float) -> str:
    r, g, b = colorsys.hls_to_rgb(h / 360, max(0, min(100, lightness)) / 100, max(0, min(100, s)) / 100)
    return "#" + "".join(f"{round(v * 255):02x}" for v in (r, g, b))


def css() -> str:
    """CSS variables for the chat (Chainlit's HSL variables) and the agent panel (--brand*). Empty without a custom
    colour, so public/custom.css (Hapilot green) stays in force."""
    look = active()
    if not look["customised"] or look["color"] == DEFAULT_COLOR:
        return ""
    h, s, lightness = _hsl(look["color"])
    fg = "0 0% 100%" if lightness < 62 else "0 0% 10%"
    return (f"/* company colour {look['color']} (agent panel → 🎨 Wygląd) */\n"
            f":root {{\n  --primary: {h} {s}% {lightness}% !important;\n  --primary-foreground: {fg} !important;\n"
            f"  --sidebar-primary: {h} {s}% {max(lightness - 11, 5)}% !important;\n"
            f"  --sidebar-accent: {h} 30% 90% !important;\n  --sidebar-accent-foreground: {h} {s}% 25% !important;\n"
            f"  --ring: {h} {s}% {lightness}% !important;\n"
            f"  --brand: {look['color']}; --brand-strong: {_hex(h, s, lightness - 12)}; --brand-soft: {_hex(h, 45, 93)};\n}}\n"
            f".dark {{\n  --primary: {h} {s}% {lightness}% !important;\n  --primary-foreground: {fg} !important;\n"
            f"  --sidebar-primary: {h} {s}% {min(lightness + 9, 90)}% !important;\n"
            f"  --sidebar-accent: {h} 20% 18% !important;\n  --sidebar-accent-foreground: {h} 40% 80% !important;\n"
            f"  --ring: {h} {s}% {lightness}% !important;\n}}\n"
            f"@media (prefers-color-scheme: dark) {{ :root:not([data-theme=\"light\"]) {{ --brand-soft: {_hex(h, 30, 15)}; }} }}\n"
            f":root[data-theme=\"dark\"] {{ --brand-soft: {_hex(h, 30, 15)}; }}\n")
