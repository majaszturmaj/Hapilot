"""The company look in Chainlit's routes (src/ui_branding.py): logo, favicon, avatar, translations, stylesheets.
Needs chainlit (installed on the GPU host, not in the CI test environment)."""

import base64

import pytest

pytest.importorskip("chainlit")

from starlette.applications import Starlette  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from src import branding, ui_branding  # noqa: E402
from src.config import reset_settings  # noqa: E402
from src.source_view import png  # noqa: E402

LOGO = png(bytes([90]) * (64 * 32), 64, 32, 64, 1)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("HAPILOT_DATA_DIR", str(tmp_path / "var"))
    reset_settings()
    branding.disable()
    app = Starlette(routes=[Route(path, endpoint) for path, endpoint in ui_branding.ROUTES])
    yield TestClient(app)
    branding.disable()


def _brand(**extra):
    branding.save({"name": "Asystent Przykładowej", "description": "Opis.", "color": "#1e5aa8", **extra}, "admin")
    branding.enable_for_ui()


def test_without_a_custom_look_chainlit_serves_its_own(client):
    logo = client.get("/logo?theme=dark")
    assert logo.status_code == 200 and logo.content != LOGO
    assert client.get("/hapilot/branding.css").text == ""
    assert "--primary: 79 62% 41%" in client.get("/hapilot/style.css").text  # public/custom.css (Hapilot green)


def test_custom_logo_everywhere_and_never_cached(client):
    _brand(logo="data:image/png;base64," + base64.b64encode(LOGO).decode())
    for path in ("/logo?theme=light", "/logo?theme=dark", "/favicon", "/avatars/Asystent%20Przyk%C5%82adowej",
                 "/avatars/default", "/avatars/Hapilot"):
        res = client.get(path)
        assert res.status_code == 200 and res.content == LOGO, path
        assert res.headers["cache-control"] == "no-cache" and res.headers["content-type"] == "image/png"
    assert client.get("/avatars/kroki").content != LOGO  # other authors: Chainlit's own avatar


def test_colour_and_translations(client):
    _brand()
    style = client.get("/hapilot/style.css").text
    assert style.index("79 62% 41%") < style.index("214 70% 39%")  # the company colour comes after Hapilot's green
    assert "--brand: #1e5aa8" in client.get("/hapilot/branding.css").text
    tr = client.get("/project/translations?language=pl-PL").json()["translation"]
    flat = str(tr)
    assert "Zaloguj się — Asystent Przykładowej" in flat and "O aplikacji" in flat and "Hapilo" not in flat
    assert client.get("/project/translations?language=pl-PL;evil").status_code == 422
