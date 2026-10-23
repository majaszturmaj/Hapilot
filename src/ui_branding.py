"""The company look (src/branding.py) in Chainlit's UI.

``install()`` (src/app.py, with login on) puts Hapilot's routes ahead of Chainlit's for the logo, the favicon,
the assistant's avatar and the UI translations, serves the chat's stylesheet with the company colour
(``/hapilot/style.css`` = public/custom.css + the colour) and the colour alone for the agent panel
(``/hapilot/branding.css``), and wraps Chainlit's loader of the "O aplikacji" page. These routes are public, as
Chainlit's own: the login page needs them. Every response is ``no-cache``, so a change shows on the next page load.
"""

from __future__ import annotations

import re

from src import branding
from src.config import PROJECT_ROOT

CUSTOM_CSS = PROJECT_ROOT / "public" / "custom.css"
HEADERS = {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"}
_LANGUAGE = re.compile(r"[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})*")
_installed = False


def _logo_response():
    from starlette.responses import FileResponse

    path = branding.logo_path()
    if path is None:
        return None
    return FileResponse(path, media_type="image/png" if path.suffix == ".png" else "image/jpeg", headers=HEADERS)


async def logo(request):
    custom = _logo_response()
    if custom is not None:
        return custom
    from chainlit.server import get_logo
    from chainlit.types import Theme

    theme = request.query_params.get("theme", "light")
    return await get_logo(Theme.dark if theme == "dark" else Theme.light)


async def favicon(request):
    custom = _logo_response()
    if custom is not None:
        return custom
    from chainlit.server import get_favicon

    return await get_favicon()


def _avatar_key(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace(".", "_")


async def avatar(request):
    """The assistant's avatar (its name, the default, or "Hapilot" in older chats) is the company logo."""
    avatar_id = request.path_params["avatar_id"]
    if _avatar_key(avatar_id) in {"default", _avatar_key(branding.name()), "hapilot"}:
        custom = _logo_response()
        if custom is not None:
            return custom
    from chainlit.server import get_avatar

    return await get_avatar(avatar_id)


async def translations(request):
    from chainlit.config import config
    from starlette.responses import JSONResponse

    language = request.query_params.get("language", "en-US")
    if not _LANGUAGE.fullmatch(language):
        return JSONResponse({"detail": "Invalid language"}, status_code=422)

    def walk(value):
        if isinstance(value, str):
            return branding.ui_text(value)
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    return JSONResponse({"translation": walk(config.load_translation(config.ui.language or language))}, headers=HEADERS)


async def style(request):
    from starlette.responses import Response

    base = CUSTOM_CSS.read_text(encoding="utf-8") if CUSTOM_CSS.is_file() else ""
    return Response(base + "\n" + branding.css(), media_type="text/css; charset=utf-8", headers=HEADERS)


async def colour(request):
    from starlette.responses import Response

    return Response(branding.css(), media_type="text/css; charset=utf-8", headers=HEADERS)


ROUTES = [("/logo", logo), ("/favicon", favicon), ("/avatars/{avatar_id:str}", avatar),
          ("/project/translations", translations), ("/hapilot/style.css", style), ("/hapilot/branding.css", colour)]


def install() -> None:
    """Routes ahead of Chainlit's, the stylesheet with the company colour, the "O aplikacji" page with the name."""
    global _installed
    if _installed:
        return
    import chainlit.server as server
    from chainlit.config import config
    from starlette.routing import Route

    server.app.router.routes[0:0] = [Route(path, endpoint, methods=["GET"]) for path, endpoint in ROUTES]
    original_markdown = server.get_markdown_str

    def markdown(root, language):
        text = original_markdown(root, language)
        return branding.markdown(text) if text else text

    server.get_markdown_str = markdown  # used by /project/settings for the "O aplikacji" page
    config.ui.custom_css = "/hapilot/style.css"
    branding.enable_for_ui()
    _installed = True
