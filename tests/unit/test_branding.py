"""Company look (src/branding.py): administrators only, checks, history and restore, the name and description in
the prompts of the UI process only, colour CSS, UI texts and the "O aplikacji" page."""

import base64
import json

import pytest

from src import admin, branding
from src.config import reset_settings
from src.source_view import png


def _png(width=120, height=40) -> bytes:
    return png(bytes([200]) * (width * height), width, height, width, 1)


def _data_url(raw: bytes, mime="image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HAPILOT_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.delenv("HAPILOT_UI_ADMINS", raising=False)  # default: "admin"
    reset_settings()
    branding.disable()
    yield tmp_path / "var"
    branding.disable()


LOOK = {"name": "Asystent Przykładowej", "description": "Przykładowa Sp. z o.o. serwisuje maszyny górnicze.",
        "color": "#1e5aa8"}


def test_defaults_without_any_change():
    assert branding.current()["customised"] is False and branding.name() == "Hapilot"
    assert branding.css() == "" and branding.logo_path() is None
    assert branding.apply_to_prompt("Jesteś Hapilot.", "chat") == "Jesteś Hapilot."


def test_only_administrators_change_the_look():
    with pytest.raises(branding.BrandingError, match="administrator"):
        branding.save(LOOK, "uzytkownik")
    with pytest.raises(branding.BrandingError, match="administrator"):
        branding.reset("uzytkownik")
    info = branding.info("uzytkownik")
    assert info["admin"] is False and "history" not in info


def test_save_logo_history_audit_and_versioned_url(data_dir):
    info = branding.save({**LOOK, "logo": _data_url(_png())}, "admin")
    assert info["name"] == LOOK["name"] and info["customised"] and info["color"] == "#1e5aa8"
    path = branding.logo_path()
    assert path.is_file() and path.suffix == ".png" and path.read_bytes() == _png()
    assert info["logo_url"].startswith("/logo?v=") and len(info["history"]) == 1
    entry = admin.audit_log()[0]
    assert entry["action"] == "branding_saved" and entry["user"] == "admin" and "logo" in entry["detail"]

    again = branding.save({**LOOK}, "admin")  # same values, logo kept (no "logo" key): no new version
    assert len(again["history"]) == 1 and branding.logo_path() == path
    branding.save({**LOOK, "logo": "remove"}, "admin")
    assert branding.logo_path() is None and len(branding.history()) == 2


@pytest.mark.parametrize("change, message", [
    ({"name": "A"}, "od 2 do"),
    ({"name": "x" * 41}, "od 2 do"),
    ({"name": "<script>alert(1)</script>"}, "litery"),
    ({"name": 'Firma "X"'}, "litery"),
    ({"description": "x" * 301}, "za długi"),
    ({"description": "Firma <b>X</b>"}, "< ani >"),
    ({"color": "red"}, "#RRGGBB"),
    ({"logo": _data_url(b"<svg xmlns='http://www.w3.org/2000/svg'><script>x</script></svg>", "image/svg+xml")}, "SVG"),
    ({"logo": _data_url(_png(10, 10))}, "Nietypowy rozmiar"),
    ({"logo": _data_url(b"\x89PNG\r\n\x1a\n" + b"0" * (1024 * 1024 + 1))}, "za duże"),
    ({"logo": "not base64 !!"}, "odczytać"),
])
def test_bad_values_are_refused(change, message):
    with pytest.raises(branding.BrandingError, match=message):
        branding.save({**LOOK, **change}, "admin")
    assert branding.history() == []


def test_names_are_normalised():
    assert branding.check_name("  Asystent   Firmy-X  ") == "Asystent Firmy-X"
    assert branding.check_description(" Jedno  zdanie.\n Drugie. ") == "Jedno zdanie. Drugie."


def test_reset_and_restore():
    branding.save({**LOOK, "logo": _data_url(_png())}, "admin")
    branding.save({**LOOK, "name": "Druga Nazwa", "logo": "remove"}, "admin")
    assert branding.reset("admin")["customised"] is False and branding.current()["name"] == "Hapilot"
    info = branding.restore(0, "admin")  # the first version, with its logo, as a new version
    assert info["name"] == LOOK["name"] and branding.logo_path() is not None and len(branding.history()) == 4
    with pytest.raises(branding.BrandingError):
        branding.restore(2, "admin")  # the reset entry
    with pytest.raises(branding.BrandingError):
        branding.restore(99, "admin")
    actions = [e["action"] for e in admin.audit_log()]
    assert actions[:2] == ["branding_saved", "branding_reset"]


def test_prompts_get_name_and_description_in_the_ui_process_only():
    branding.save(LOOK, "admin")
    prompt = "Jesteś Hapilot — asystent. Pozostań w roli Hapilota. O Hapilocie: lokalny. hapilot_extension.json"
    assert branding.apply_to_prompt(prompt, "chat") == prompt  # evaluation runs and scripts: never enabled
    branding.enable_for_ui()
    chat = branding.apply_to_prompt(prompt, "chat")
    assert "Hapilot" not in chat.replace("hapilot_extension", "")
    assert chat.startswith("Jesteś Asystent Przykładowej — asystent. Pozostań w roli Asystent Przykładowej.")
    assert "[FIRMA]\nPrzykładowa Sp. z o.o. serwisuje maszyny górnicze.\nPrzedstawiając się, używaj nazwy: Asystent Przykładowej." in chat
    coder = branding.apply_to_prompt("You are Hapilot — Python engineer.", "coder")
    assert coder == "You are Asystent Przykładowej — Python engineer."  # name only, no company context


def test_get_llm_system_prompt(llm):
    from src.llm_config import get_llm

    branding.save(LOOK, "admin")
    _, plain = get_llm("chat")
    assert "[FIRMA]" not in plain and "Hapilot" in plain
    branding.enable_for_ui()
    _, branded = get_llm("chat")
    assert "[FIRMA]" in branded and "Hapilot" not in branded and "Asystent Przykładowej" in branded
    assert branded.rstrip().split("\n")[-1].startswith("Aktualna data")  # the date stays last


def test_ui_texts_and_about_page():
    assert branding.ui_text("Zaloguj się do Hapilota") == "Zaloguj się do Hapilota"  # nothing set
    branding.save(LOOK, "admin")
    branding.enable_for_ui()
    assert branding.ui_text("Zaloguj się do Hapilota") == "Zaloguj się — Asystent Przykładowej"
    assert branding.ui_text("O Hapilocie") == "O aplikacji"
    about = branding.markdown("# Hapilot — Twój pomocny asystent AI 👋\nJestem **Hapilot**.")
    assert about.startswith("# Asystent Przykładowej — Twój") and "**Asystent Przykładowej**" in about
    assert about.rstrip().endswith(branding.CREDIT)


def test_colour_css():
    branding.save(LOOK, "admin")
    branding.enable_for_ui()
    css = branding.css()
    assert "--primary: 214 70% 39% !important" in css and "--brand: #1e5aa8" in css and ".dark {" in css
    branding.save({**LOOK, "color": branding.DEFAULT_COLOR}, "admin")
    assert branding.css() == ""  # Hapilot green comes from public/custom.css


def test_icons_follow_the_logo_shape():
    assert branding.icon() is None and branding.wordmark_svg() is None  # Hapilot: Chainlit's own files
    branding.save({**LOOK, "logo": _data_url(_png(48, 48))}, "admin")
    branding.enable_for_ui()
    kind, value = branding.icon()
    assert kind == "logo" and value == branding.logo_path() and branding.wordmark_svg() is None
    branding.save({**LOOK, "logo": _data_url(_png(240, 60))}, "admin")
    kind, value = branding.icon()
    assert kind == "svg" and ">A</text>" in value and 'fill="#1e5aa8"' in value
    branding.save({**LOOK, "logo": "remove"}, "admin")
    assert "Asystent Przykładowej</text>" in branding.wordmark_svg()


def test_storage_is_plain_json(data_dir):
    branding.save(LOOK, "admin")
    saved = json.loads((data_dir / "branding" / "branding.json").read_text(encoding="utf-8"))
    assert saved["history"][0]["name"] == LOOK["name"] and saved["history"][0]["user"] == "admin"
