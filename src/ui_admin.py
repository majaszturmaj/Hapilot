"""Agent panel routes (``/hapilot/admin``): the page, its assets and a small JSON API over src/admin.py.

Who may use it: any user logged in to the Chainlit UI (its ``access_token`` cookie, checked with Chainlit's
own JWT decoder). What each user may do is decided in src/admin.py (own private nodes for everyone; global
nodes and built-in prompts for administrators). Requests that change something must also come from the
panel's own origin (the ``Origin`` header, when sent, must match the host) and carry the ``X-Hapilot-Admin``
header, which a cross-site form cannot set.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from src import admin

PREFIX = "/hapilot/admin"
ASSETS = Path(__file__).resolve().parent / "admin_ui"
ASSET_TYPES = {"admin.css": "text/css; charset=utf-8", "admin.js": "text/javascript; charset=utf-8"}
NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Referrer-Policy": "same-origin"}


def _identifier(request) -> Optional[str]:
    from chainlit.auth.cookie import get_token_from_cookies
    from chainlit.auth.jwt import decode_jwt

    token = get_token_from_cookies(dict(request.cookies))
    if not token:
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else None
    if not token:
        return None
    try:
        return decode_jwt(token).identifier
    except Exception:
        return None


def _json(data, status: int = 200):
    from starlette.responses import JSONResponse

    return JSONResponse(data, status_code=status, headers=NO_STORE)


def _same_origin(request) -> bool:
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host", ""):
        return False
    return request.headers.get("x-hapilot-admin") == "1"


async def _guard(request, write: bool = False, admin_only: bool = False):
    """(identifier, None) for an allowed user, else (None, error response)."""
    user = _identifier(request)
    if user is None:
        return None, _json({"error": "Zaloguj się ponownie."}, 401)
    if admin_only and not admin.is_admin(user):
        return None, _json({"error": "Tylko administrator może to zrobić."}, 403)
    if write and not _same_origin(request):
        return None, _json({"error": "Żądanie spoza panelu administratora zostało odrzucone."}, 403)
    return user, None


async def _call(fn, *args):
    try:
        return _json(await asyncio.to_thread(fn, *args))
    except admin.AdminError as exc:
        return _json({"error": str(exc)}, 400)


# --------------------------------------------------------------------------- page

async def page(request):
    from starlette.responses import HTMLResponse, RedirectResponse

    if _identifier(request) is None:
        return RedirectResponse("/login", status_code=302)
    return HTMLResponse((ASSETS / "index.html").read_text(encoding="utf-8"), headers=NO_STORE)


async def asset(request):
    from starlette.responses import Response

    name = request.path_params["name"]
    if name not in ASSET_TYPES:
        return Response(status_code=404)
    return Response((ASSETS / name).read_bytes(), media_type=ASSET_TYPES[name],
                    headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})


# --------------------------------------------------------------------------- API

async def me(request):
    user = _identifier(request)
    if user is None:
        return _json({"error": "Niezalogowany"}, 401)
    return _json({"identifier": user, "admin": admin.is_admin(user)})


async def graph(request):
    user, error = await _guard(request)
    return error or await _call(admin.graph_data, user)


async def prompts(request):
    user, error = await _guard(request)
    return error or await _call(admin.list_prompts)


async def prompt(request):
    role = request.path_params["role"]
    if request.method == "GET":
        user, error = await _guard(request)
        return error or await _call(admin.prompt_info, role)
    user, error = await _guard(request, write=True, admin_only=True)
    if error:
        return error
    if request.method == "DELETE":
        return await _call(admin.restore_prompt, role, user)
    body = await request.json()
    return await _call(admin.save_prompt, role, str(body.get("text", "")), user)


async def nodes(request):
    user, error = await _guard(request, write=True)
    if error:
        return error
    body = await request.json()
    return await _call(admin.create_custom_node, body if isinstance(body, dict) else {}, user)


async def node(request):
    user, error = await _guard(request, write=True)
    if error:
        return error
    name = request.path_params["name"]
    if request.method == "PUT":
        body = await request.json()
        return await _call(admin.update_custom_node, name, body if isinstance(body, dict) else {}, user)

    def delete(name: str, who: str) -> dict:
        admin.delete_custom_node(name, who)
        return {"deleted": name}

    return await _call(delete, name, user)


async def share(request):
    user, error = await _guard(request, write=True, admin_only=True)
    if error:
        return error
    body = await request.json()
    return await _call(admin.set_global, request.path_params["name"], bool((body or {}).get("global")), user)


async def audit(request):
    user, error = await _guard(request)
    return error or await _call(admin.audit_log, user)


ROUTES = [
    ("", page, ["GET"]),
    ("/assets/{name}", asset, ["GET"]),
    ("/api/me", me, ["GET"]),
    ("/api/graph", graph, ["GET"]),
    ("/api/prompts", prompts, ["GET"]),
    ("/api/prompts/{role}", prompt, ["GET", "PUT", "DELETE"]),
    ("/api/nodes", nodes, ["POST"]),
    ("/api/nodes/{name}", node, ["PUT", "DELETE"]),
    ("/api/nodes/{name}/global", share, ["PUT"]),
    ("/api/audit", audit, ["GET"]),
]
_installed = False


def install_routes() -> None:
    """Add the routes to Chainlit's server ahead of its catch-all page route."""
    global _installed
    if _installed:
        return
    from starlette.routing import Route

    from chainlit.server import app

    new = [Route(PREFIX + path, endpoint, methods=methods) for path, endpoint, methods in ROUTES]
    app.router.routes[0:0] = new
    _installed = True
