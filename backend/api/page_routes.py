import asyncio
import importlib.util
import logging
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from ..config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/personalised-pages", tags=["personalised-pages"])

_PAGES_DIR = Path("data/personalised_pages")
_SHARED_DIR = _PAGES_DIR / "_shared"


def _validate_page_id(page_id: str) -> None:
    if not re.fullmatch(r'[a-z0-9_]{1,64}', page_id):
        raise HTTPException(status_code=400, detail="Invalid page_id")
    resolved = (_PAGES_DIR / page_id).resolve()
    if not str(resolved).startswith(str(_PAGES_DIR.resolve())):
        raise HTTPException(status_code=400, detail="Invalid page_id")


_shared_version_cache: str = ""
_shared_version_mtime: float = 0.0

def _shared_version() -> str:
    global _shared_version_cache, _shared_version_mtime
    import hashlib
    css = _SHARED_DIR / "hime-ui.css"
    js = _SHARED_DIR / "hime-ui.js"
    try:
        mtime = max(css.stat().st_mtime if css.exists() else 0,
                    js.stat().st_mtime if js.exists() else 0)
        if mtime != _shared_version_mtime:
            content = (css.read_bytes() if css.exists() else b"") + (js.read_bytes() if js.exists() else b"")
            _shared_version_cache = hashlib.md5(content).hexdigest()[:8]
            _shared_version_mtime = mtime
    except Exception:
        _shared_version_cache = "1"
    return _shared_version_cache


_SHARED_CSS_LINK_RE = re.compile(
    r'<link\b[^>]*href\s*=\s*["\'][^"\']*_shared/hime-ui\.css[^"\']*["\'][^>]*>',
    re.IGNORECASE,
)
_SHARED_JS_SCRIPT_RE = re.compile(
    r'<script\b[^>]*src\s*=\s*["\'][^"\']*_shared/hime-ui\.js[^"\']*["\'][^>]*>\s*</script\s*>',
    re.IGNORECASE,
)

_shared_assets_cache: tuple[str, str] = ("", "")
_shared_assets_mtime: float = -1.0


def _read_shared_assets() -> tuple[str, str]:
    global _shared_assets_cache, _shared_assets_mtime
    css = _SHARED_DIR / "hime-ui.css"
    js = _SHARED_DIR / "hime-ui.js"
    try:
        mtime = max(css.stat().st_mtime if css.exists() else 0,
                    js.stat().st_mtime if js.exists() else 0)
        if mtime != _shared_assets_mtime:
            _shared_assets_cache = (
                css.read_text(encoding="utf-8") if css.exists() else "",
                js.read_text(encoding="utf-8") if js.exists() else "",
            )
            _shared_assets_mtime = mtime
    except Exception:
        logger.warning("Could not read shared page assets for inlining", exc_info=True)
        return ("", "")
    return _shared_assets_cache


def _inline_shared_assets(html: str) -> str:
    css_text, js_text = _read_shared_assets()
    if css_text:
        safe_css = css_text.replace("</style", "<\\/style")
        html = _SHARED_CSS_LINK_RE.sub(
            lambda _m: f"<style>\n{safe_css}\n</style>", html, count=1
        )
    if js_text:
        safe_js = js_text.replace("</script", "<\\/script")
        html = _SHARED_JS_SCRIPT_RE.sub(
            lambda _m: f"<script>\n{safe_js}\n</script>", html, count=1
        )
    return html


def _get_db_file(user_id: str) -> Path:
    return settings.MEMORY_DB_PATH / f"{user_id}.db"


def _get_active_pid() -> str | None:
    try:
        from .agent_state import active_agents
        if active_agents:
            return next(iter(active_agents))
    except Exception:
        pass
    return None


def _all_memory_dbs() -> list[Path]:
    db_dir = settings.MEMORY_DB_PATH
    if not db_dir.exists():
        return []
    return list(db_dir.glob("*.db"))


@router.get("/_shared/{filename}")
async def serve_shared_asset(filename: str):
    from fastapi.responses import Response
    content_types = {".css": "text/css", ".js": "application/javascript"}
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', filename) or not filename.endswith(tuple(content_types)):
        raise HTTPException(status_code=400, detail="Invalid asset name")
    path = _SHARED_DIR / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"Shared asset '{filename}' not found")
    ct = content_types[path.suffix]
    return Response(
        content=await asyncio.to_thread(path.read_text, encoding="utf-8"),
        media_type=ct,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache"},
    )


@router.get("/list")
async def list_personalised_pages():
    def _scan() -> list[dict]:
        pages: list[dict] = []
        seen_ids: set[str] = set()

        for db_file in _all_memory_dbs():
            try:
                with sqlite3.connect(str(db_file), timeout=10) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT page_id, display_name, description, backend_route, "
                        "frontend_asset, created_at "
                        "FROM personalised_pages WHERE status='active' ORDER BY created_at ASC"
                    ).fetchall()
                for row in rows:
                    d = dict(row)
                    if d["page_id"] not in seen_ids:
                        seen_ids.add(d["page_id"])
                        pages.append(d)
            except Exception:
                continue
        return pages

    return {"success": True, "pages": await asyncio.to_thread(_scan)}


@router.delete("/{page_id}")
async def delete_personalised_page(page_id: str):
    _validate_page_id(page_id)

    def _mark_deleted() -> bool:
        marked = False
        for db_file in _all_memory_dbs():
            try:
                with sqlite3.connect(str(db_file), timeout=10) as conn:
                    cur = conn.execute(
                        "UPDATE personalised_pages SET status='deleted' WHERE page_id=? AND status='active'",
                        (page_id,),
                    )
                    if cur.rowcount > 0:
                        marked = True
            except Exception:
                continue
        return marked

    updated = await asyncio.to_thread(_mark_deleted)

    if page_id == "_shared":
        raise HTTPException(status_code=400, detail="Cannot delete shared assets")
    page_dir = _PAGES_DIR / page_id
    if page_dir.exists():
        await asyncio.to_thread(shutil.rmtree, page_dir, ignore_errors=True)
        updated = True

    if not updated:
        raise HTTPException(status_code=404, detail=f"Page '{page_id}' not found")

    logger.info("Deleted personalised page: %s", page_id)
    return {"success": True, "page_id": page_id}


_PAGE_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'self'; "
    "form-action 'self'; "
    "base-uri 'none'"
)
_PAGE_SECURITY_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Content-Security-Policy": _PAGE_CSP,
    "X-Frame-Options": "SAMEORIGIN",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

_PAGE_CSP_META = "; ".join(
    d for d in _PAGE_CSP.split("; ") if not d.startswith("frame-ancestors")
)
_PAGE_META_TAGS = (
    f'<meta http-equiv="Content-Security-Policy" content="{_PAGE_CSP_META}">'
    '<meta name="referrer" content="no-referrer">'
)


def _inject_head(html: str, snippet: str) -> str:
    lowered = html.lower()
    idx = lowered.find("<head>")
    if idx != -1:
        cut = idx + len("<head>")
        return html[:cut] + "\n    " + snippet + html[cut:]
    idx = lowered.find("<html")
    if idx != -1:
        end = html.find(">", idx)
        if end != -1:
            return html[: end + 1] + "\n" + snippet + html[end + 1:]
    return snippet + html


@router.get("/{page_id}/", response_class=HTMLResponse)
async def serve_page_frontend(page_id: str):
    _validate_page_id(page_id)
    html_path = _PAGES_DIR / page_id / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail=f"Page '{page_id}' not found")
    html = await asyncio.to_thread(html_path.read_text, encoding="utf-8")
    html = await asyncio.to_thread(_inline_shared_assets, html)
    html = html.replace(
        '/api/personalised-pages/_shared/hime-ui.css',
        f'/api/personalised-pages/_shared/hime-ui.css?v={_shared_version()}',
    ).replace(
        '/api/personalised-pages/_shared/hime-ui.js',
        f'/api/personalised-pages/_shared/hime-ui.js?v={_shared_version()}',
    )
    if 'name="viewport"' not in html and "name='viewport'" not in html:
        html = _inject_head(
            html,
            '<meta name="viewport" '
            'content="width=device-width, initial-scale=1, '
            'viewport-fit=cover">',
        )
    html = _inject_head(html, _PAGE_META_TAGS)
    return HTMLResponse(content=html, headers=_PAGE_SECURITY_HEADERS)


_PAGE_HANDLER_TIMEOUT_S = 20.0

_module_cache: dict[str, tuple[tuple[int, int], Any]] = {}


def _load_page_module(page_id: str, route_path: Path) -> Any:
    st = route_path.stat()
    key = (st.st_mtime_ns, st.st_size)
    cached = _module_cache.get(page_id)
    if cached is not None and cached[0] == key:
        return cached[1]
    source = route_path.read_text(encoding="utf-8")
    compile(source, str(route_path), "exec")
    spec = importlib.util.spec_from_file_location(
        f"personalised_page_{page_id}", route_path
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _module_cache[page_id] = (key, mod)
    return mod


async def _exec_route_handler(page_id: str, request: Request | None):
    _validate_page_id(page_id)
    route_path = _PAGES_DIR / page_id / "route.py"
    if not route_path.exists():
        _module_cache.pop(page_id, None)
        return JSONResponse(
            status_code=404,
            content={"success": False, "error": f"Page '{page_id}' backend not found"},
        )

    try:
        module = await asyncio.wait_for(
            asyncio.to_thread(_load_page_module, page_id, route_path),
            timeout=_PAGE_HANDLER_TIMEOUT_S,
        )
    except SyntaxError as se:
        logger.error(
            "serve_page_data syntax error for %s: %s (line %s)",
            page_id, se.msg, se.lineno,
        )
        return JSONResponse(
            status_code=422,
            content={
                "success": False,
                "error": f"Page '{page_id}' has a syntax error in its backend code: "
                         f"{se.msg} (line {se.lineno}). "
                         "Ask the agent to recreate this page.",
            },
        )
    except asyncio.TimeoutError:
        logger.error("serve_page_data: loading backend for %s timed out", page_id)
        return JSONResponse(
            status_code=504,
            content={"success": False, "error": f"Page '{page_id}' backend timed out while loading."},
        )
    except Exception as e:
        logger.error("serve_page_data load error for %s: %s", page_id, e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": f"Page '{page_id}' backend encountered an internal error.",
            },
        )

    try:
        if not hasattr(module, "route_handler"):
            return JSONResponse(
                status_code=500,
                content={
                    "success": False,
                    "error": f"Page '{page_id}' backend has no route_handler function.",
                },
            )

        async def _run():
            if asyncio.iscoroutinefunction(module.route_handler):
                return await module.route_handler(request)
            body = await request.body()
            request._body = body
            return await asyncio.to_thread(module.route_handler, request)

        result = await asyncio.wait_for(_run(), timeout=_PAGE_HANDLER_TIMEOUT_S)
        return JSONResponse(content=result)
    except asyncio.TimeoutError:
        logger.error(
            "serve_page_data: route_handler for %s exceeded %.0fs", page_id,
            _PAGE_HANDLER_TIMEOUT_S,
        )
        return JSONResponse(
            status_code=504,
            content={
                "success": False,
                "error": f"Page '{page_id}' backend timed out.",
            },
        )
    except Exception as e:
        logger.error("serve_page_data error for %s: %s", page_id, e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": f"Page '{page_id}' backend encountered an internal error.",
            },
        )


@router.get("/{page_id}/data")
async def serve_page_data_get(page_id: str, request: Request):
    return await _exec_route_handler(page_id, request)


@router.post("/{page_id}/data")
async def serve_page_data_post(page_id: str, request: Request):
    return await _exec_route_handler(page_id, request)
