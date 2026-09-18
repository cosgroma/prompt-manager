"""aiohttp application factory for PromptManager.

:func:`create_app` wires together the pieces the rest of the package provides:
configuration, the database, the REST API routes, middleware, and the
filesystem image monitor (started and stopped with the application).

Nothing here runs at import time, so tests can build isolated application
instances against temporary databases.

Example:
    from aiohttp import web
    from promptmanager.app import create_app

    web.run_app(create_app(), host="127.0.0.1", port=8189)
"""

import hmac
from typing import Optional

from aiohttp import web

from . import config
from .api import PromptManagerAPI, gzip_middleware
from .database.operations import PromptDatabase
from .ingest.image_monitor import ImageMonitor
from .utils.logging_config import get_logger

LANDING_PATH = "/prompt_manager/gallery"
TOKEN_COOKIE = "pm_token"
TOKEN_HEADER = "X-PM-Token"

# Maximum accepted request body. /prompt_manager/restore uploads whole
# database backups, which aiohttp's 1 MB default would reject.
DEFAULT_CLIENT_MAX_SIZE = 1024 * 1024 * 1024  # 1 GB

API_KEY = web.AppKey("promptmanager_api", PromptManagerAPI)
MONITOR_KEY = web.AppKey("promptmanager_monitor", ImageMonitor)

logger = get_logger("prompt_manager.app")


def make_token_middleware(token: str):
    """Build middleware that gates every request behind a shared token.

    The token may arrive as a ``token`` query parameter or an ``X-PM-Token``
    header; on success it is echoed back as a cookie so the web UI works
    without modification for the rest of the session.

    Args:
        token: The secret to require.

    Returns:
        An aiohttp middleware coroutine.
    """
    expected = token.encode("utf-8")

    def matches(candidate):
        # Constant-time comparison so response timing does not leak the token.
        if not candidate:
            return False
        return hmac.compare_digest(candidate.encode("utf-8"), expected)

    @web.middleware
    async def token_middleware(request, handler):
        if matches(request.cookies.get(TOKEN_COOKIE)):
            return await handler(request)

        supplied = request.query.get("token") or request.headers.get(TOKEN_HEADER)
        if matches(supplied):
            response = await handler(request)
            response.set_cookie(
                TOKEN_COOKIE, token, httponly=True, samesite="Lax", path="/"
            )
            return response

        return web.json_response(
            {"error": "Unauthorized. Append ?token=... to the URL."}, status=401
        )

    return token_middleware


def create_app(
    token: Optional[str] = None,
    db_path: Optional[str] = None,
    load_config: bool = True,
    start_monitor: bool = True,
    client_max_size: int = DEFAULT_CLIENT_MAX_SIZE,
) -> web.Application:
    """Build the PromptManager web application.

    Args:
        token: Optional shared secret to require on every request.
        db_path: SQLite database file. Defaults to PROMPT_MANAGER_DB, then the
            configured path, then prompts.db in the data directory.
        load_config: Load config.json and environment overrides first. Pass
            False when the caller has already configured the package.
        start_monitor: Watch the configured output directories while the
            application runs (still subject to GalleryConfig.MONITORING_ENABLED).
        client_max_size: Maximum accepted request body in bytes.

    Returns:
        A configured aiohttp Application ready to serve.
    """
    if load_config:
        config.load()

    middlewares = []
    if token:
        middlewares.append(make_token_middleware(token))
    middlewares.append(gzip_middleware)

    app = web.Application(client_max_size=client_max_size, middlewares=middlewares)

    db = PromptDatabase(db_path)
    api = PromptManagerAPI(db=db)
    monitor = ImageMonitor(db, on_ingest=api.invalidate_gallery_cache)
    api.monitor = monitor

    routes = web.RouteTableDef()
    api.add_routes(routes)
    app.add_routes(routes)

    async def redirect_to_gallery(request):
        raise web.HTTPFound(LANDING_PATH)

    app.router.add_get("/", redirect_to_gallery)

    app[API_KEY] = api
    app[MONITOR_KEY] = monitor

    if start_monitor:
        app.on_startup.append(_start_monitor)
    app.on_cleanup.append(_stop_monitor)

    return app


async def _start_monitor(app: web.Application):
    """Start the filesystem image monitor when the application starts."""
    try:
        app[MONITOR_KEY].start_monitoring()
    except Exception as exc:
        logger.error(f"Failed to start image monitoring: {exc}")


async def _stop_monitor(app: web.Application):
    """Stop the filesystem image monitor when the application shuts down."""
    try:
        app[MONITOR_KEY].stop_monitoring()
    except Exception as exc:  # pragma: no cover - best-effort cleanup
        logger.warning(f"Failed to stop image monitor: {exc}")
