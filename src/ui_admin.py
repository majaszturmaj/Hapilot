"""Agent panel routes (``/hapilot/admin``): the page, its assets and a small JSON API over src/admin.py.

Who may use it: any user logged in to the Chainlit UI (its ``access_token`` cookie, checked with Chainlit's
own JWT decoder). What each user may do is decided in src/admin.py (own private nodes for everyone; global
nodes and built-in prompts for administrators). Requests that change something must also come from the
panel's own origin (the ``Origin`` header, when sent, must match the host) and carry the ``X-Hapilot-Admin``
header, which a cross-site form cannot set. The knowledge base (``/api/kb…``, src/knowledge_admin.py) is for
administrators only; documents are uploaded as the request body (no form encoding), up to
``HAPILOT_KB_MAX_UPLOAD_MB``.
"""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from src import admin, branding, doc_templates, knowledge_admin, signatures
from src.config import get_settings

PREFIX = "/hapilot/admin"
ASSETS = Path(__file__).resolve().parent / "admin_ui"
ASSET_TYPES = {"admin.css": "text/css; charset=utf-8", "admin.js": "text/javascript; charset=utf-8",
               "templates.js": "text/javascript; charset=utf-8", "branding.js": "text/javascript; charset=utf-8",
               "knowledge.js": "text/javascript; charset=utf-8"}
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
    except (admin.AdminError, doc_templates.TemplateError, signatures.SignatureError, branding.BrandingError,
            knowledge_admin.KnowledgeError) as exc:
        return _json({"error": str(exc)}, getattr(exc, "status", 400))


async def _body(request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


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


# --------------------------------------------------------------------------- document templates

async def templates(request):
    if request.method == "GET":
        user, error = await _guard(request)
        if error:
            return error
        return await _call(lambda u: {"templates": doc_templates.list_templates(u), **doc_templates.builder_info(),
                                      "admin": admin.is_admin(u)}, user)
    user, error = await _guard(request, write=True)
    return error or await _call(doc_templates.create_template, await _body(request), user)


async def template(request):
    tid = request.path_params["tid"]
    if request.method == "GET":
        user, error = await _guard(request)
        return error or await _call(doc_templates.get_template, tid, user)
    user, error = await _guard(request, write=True)
    if error:
        return error
    if request.method == "PUT":
        return await _call(doc_templates.update_template, tid, await _body(request), user)
    if tid in doc_templates.catalog(None)["templates"]:  # DELETE of a built-in = restore its default
        return await _call(doc_templates.restore_builtin, tid, user)
    return await _call(doc_templates.delete_template, tid, user)


async def template_share(request):
    user, error = await _guard(request, write=True, admin_only=True)
    if error:
        return error
    return await _call(doc_templates.set_global, request.path_params["tid"], bool((await _body(request)).get("global")), user)


async def template_preview(request):
    user, error = await _guard(request, write=True)  # compiles in the sandbox: same-origin requests only
    return error or await _call(doc_templates.preview, await _body(request), user)


async def signature(request):
    """The user's own signature scan (never anyone else's — administrators included)."""
    if request.method == "GET":
        user, error = await _guard(request)
        return error or await _call(signatures.info, user)
    user, error = await _guard(request, write=True)
    if error:
        return error
    if request.method == "DELETE":
        return await _call(signatures.delete, user)
    import base64
    import binascii

    try:
        data = base64.b64decode(str((await _body(request)).get("data", "")).split(",")[-1], validate=True)
    except (binascii.Error, ValueError):
        return _json({"error": "Nie udało się odczytać pliku."}, 400)
    return await _call(signatures.save, user, data)


# --------------------------------------------------------------------------- company look

async def branding_api(request):
    """GET: the current look (any user; administrators also get the history). PUT: save, DELETE: back to Hapilot
    (administrators, same-origin)."""
    if request.method == "GET":
        user, error = await _guard(request)
        return error or await _call(branding.info, user)
    user, error = await _guard(request, write=True, admin_only=True)
    if error:
        return error
    if request.method == "DELETE":
        return await _call(branding.reset, user)
    return await _call(branding.save, await _body(request), user)


async def branding_restore(request):
    user, error = await _guard(request, write=True, admin_only=True)
    return error or await _call(branding.restore, request.path_params["index"], user)


# --------------------------------------------------------------------------- knowledge base (administrators)

INLINE = {".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8"}


async def _receive(request) -> Path:
    """The request body (the file as is) in a temporary file of the knowledge base; refused above the limit."""
    limit = get_settings().kb_max_upload_mb * 1024 * 1024
    too_big = knowledge_admin.KnowledgeError(f"Plik jest za duży (najwyżej {get_settings().kb_max_upload_mb} MB).")
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise too_big
    target = knowledge_admin.incoming_file()
    size = 0
    try:
        with target.open("wb") as fh:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise too_big
                fh.write(chunk)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


async def _received(request):
    """(temporary file, None) or (None, error response)."""
    try:
        return await _receive(request), None
    except knowledge_admin.KnowledgeError as exc:
        return None, _json({"error": str(exc)}, 413)


async def kb(request):
    user, error = await _guard(request, admin_only=True)
    return error or await _call(knowledge_admin.overview, user)


async def kb_upload(request):
    """A new document (``?name=``); ``&replace=1`` replaces a document of that name."""
    user, error = await _guard(request, write=True, admin_only=True)
    if error:
        return error
    data, error = await _received(request)
    if error:
        return error
    query = request.query_params
    return await _call(knowledge_admin.upload, query.get("name", ""), data, user, query.get("replace") == "1")


async def kb_file(request):
    """``?source=``: GET the original (``&inline=1`` opens a PDF or text in the browser), PUT a new version,
    DELETE (to the trash)."""
    source = request.query_params.get("source", "")
    if request.method == "GET":
        user, error = await _guard(request, admin_only=True)
        if error:
            return error
        from starlette.responses import FileResponse

        try:
            path = await asyncio.to_thread(knowledge_admin.file_for, source, user)
        except knowledge_admin.KnowledgeError as exc:
            return _json({"error": str(exc)}, 404)
        inline = request.query_params.get("inline") == "1" and path.suffix.lower() in INLINE
        media = INLINE.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media, filename=path.name, headers=NO_STORE,
                            content_disposition_type="inline" if inline else "attachment")
    user, error = await _guard(request, write=True, admin_only=True)
    if error:
        return error
    if request.method == "DELETE":
        return await _call(knowledge_admin.delete, source, user)
    data, error = await _received(request)
    if error:
        return error
    return await _call(knowledge_admin.replace_document, source, data, user)


async def kb_text(request):
    user, error = await _guard(request, admin_only=True)
    return error or await _call(knowledge_admin.text, request.query_params.get("source", ""), user)


async def kb_rescan(request):
    user, error = await _guard(request, write=True, admin_only=True)
    if error:
        return error
    knowledge_admin.request_rescan()
    return _json({"queued": True})


async def kb_restore(request):
    user, error = await _guard(request, write=True, admin_only=True)
    return error or await _call(knowledge_admin.restore, request.query_params.get("id", ""), user)


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
    ("/api/signature", signature, ["GET", "PUT", "DELETE"]),
    ("/api/templates", templates, ["GET", "POST"]),
    ("/api/templates/preview", template_preview, ["POST"]),
    ("/api/templates/{tid}", template, ["GET", "PUT", "DELETE"]),
    ("/api/templates/{tid}/global", template_share, ["PUT"]),
    ("/api/branding", branding_api, ["GET", "PUT", "DELETE"]),
    ("/api/branding/restore/{index:int}", branding_restore, ["POST"]),
    ("/api/kb", kb, ["GET"]),
    ("/api/kb/upload", kb_upload, ["POST"]),
    ("/api/kb/file", kb_file, ["GET", "PUT", "DELETE"]),
    ("/api/kb/text", kb_text, ["GET"]),
    ("/api/kb/rescan", kb_rescan, ["POST"]),
    ("/api/kb/trash/restore", kb_restore, ["POST"]),
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
